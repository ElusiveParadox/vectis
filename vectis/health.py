"""Health checking, in two layers.

Monitor -- runs on EVERY machine. Checks the services that machine owns
(process alive + actually answering), reports them in each heartbeat, and
emits a `health` event to the team log whenever one changes state (and
once on first check, which is the "deployed and healthy" line).

Prober -- runs only on the DNS machine. Checks the stack from the outside,
the way a client sees it: does DNS answer with the edge IP, is the LB up,
does a request through the LB succeed (and which backend answered), is
each backend up directly. Also notices when a machine's heartbeat goes
stale. Results go into dashboard.PROBES and, on change, the team log.

Open-source nginx cannot do active health checks, so this is how "health
check done" gets real active evidence rather than only nginx's passive
max_fails marking.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from . import dashboard, netprobe, state, team_logs

BACKEND_PORTS = {"backend_a": 3001, "backend_b": 3002}
PROBE_INTERVAL_S = 5
PROBE_MAX_RETRIES = 3


def _pid_alive(pid: int | None) -> bool:
    """kill -0 alone says "alive" for a killed child nobody has wait()ed
    for (a zombie) -- and a fault-injected backend is exactly that, since
    the agent spawned it. So reap it if it is our child, and treat a
    zombie state as dead."""
    if not pid:
        return False
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        st = subprocess.run(["ps", "-ww", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return True
    return bool(st) and not st.startswith("Z")


def _proc_running(name: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", name], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _svc(name: str, port: int | None, running: bool, healthy: bool, ms: float | None, detail: str) -> dict:
    return {"name": name, "port": port, "running": running, "healthy": healthy,
            "ms": None if ms is None else round(ms, 1), "detail": detail}


def check_backend(name: str, data: dict) -> dict:
    port = BACKEND_PORTS[name]
    running = _pid_alive(data["services"].get(name, {}).get("pid"))
    r = netprobe.http_fetch("127.0.0.1", port, "/healthz", timeout=2)
    want = name[-1].upper()
    if r.error:
        return _svc(name, port, running, False, None, r.error)
    if r.status != 200:
        return _svc(name, port, running, False, r.total_ms, f"HTTP {r.status}")
    got = r.headers.get("x-backend")
    if got != want:
        return _svc(name, port, running, False, r.total_ms, f"port {port} answered as backend '{got}', expected '{want}'")
    return _svc(name, port, running, True, r.total_ms, "ok")


def check_dnsmasq(domain: str, data: dict) -> dict:
    running = _proc_running("dnsmasq")
    expect = data["peers"].get("edge")
    r = netprobe.dns_query_a(f"app.{domain}", "127.0.0.1", timeout=2)
    if not r.ok:
        return _svc("dnsmasq", 53, running, False, None, r.error or "no answer")
    if expect and expect not in r.addrs:
        return _svc("dnsmasq", 53, running, False, r.rtt_ms, f"answers {r.addrs}, expected edge {expect}")
    return _svc("dnsmasq", 53, running, True, r.rtt_ms, f"app.{domain} -> {r.addrs[0]}")


def check_nginx(domain: str) -> dict:
    running = _proc_running("nginx")
    r = netprobe.http_fetch("127.0.0.1", 443, "/_edge/health", host=f"app.{domain}", tls=True, timeout=2)
    if r.error:
        return _svc("nginx", 443, running, False, None, r.error)
    if r.status != 200:
        return _svc("nginx", 443, running, False, r.total_ms, f"HTTP {r.status}")
    return _svc("nginx", 443, running, True, r.total_ms, "ok")


def local_checks(role_id: str, domain: str) -> list[dict]:
    data = state.load()
    out: list[dict] = []
    if role_id in (state.ROLE_DNS, state.ROLE_DNS_EDGE):
        out.append(check_dnsmasq(domain, data))
    if role_id in (state.ROLE_EDGE, state.ROLE_DNS_EDGE):
        out.append(check_nginx(domain))
    if role_id in (state.ROLE_BACKEND_A, state.ROLE_BACKENDS):
        out.append(check_backend("backend_a", data))
    if role_id in (state.ROLE_BACKEND_B, state.ROLE_BACKENDS):
        out.append(check_backend("backend_b", data))
    return out


class Monitor:
    def __init__(self, role_id: str, ip: str, mac: str, domain: str, dns_ip: str,
                 smoke_ok: bool, smoke_checks: dict) -> None:
        self.role_id, self.ip, self.mac, self.domain, self.dns_ip = role_id, ip, mac, domain, dns_ip
        self.smoke_ok, self.smoke_checks = smoke_ok, smoke_checks
        self.started = time.time()
        self._prev: dict[str, tuple[bool, bool]] = {}

    def _emit(self, line: str) -> None:
        team_logs.emit(self.dns_ip, self.ip, "health", line)

    def check(self) -> dict:
        """Called by the heartbeat loop every tick; returns its payload."""
        services = local_checks(self.role_id, self.domain)
        for s in services:
            key = (s["running"], s["healthy"])
            if self._prev.get(s["name"]) != key:
                first = s["name"] not in self._prev
                state_word = "UP" if s["healthy"] else "DOWN"
                port = f":{s['port']}" if s["port"] else ""
                if s["healthy"]:
                    extra = f" {s['ms']}ms" + (" (first health check passed)" if first else " (recovered)")
                else:
                    extra = f" process={'alive' if s['running'] else 'NOT running'}: {s['detail']}"
                self._emit(f"{state_word} {s['name']} {port}{extra}")
                self._prev[s["name"]] = key
        site = state.load().get("site")
        return {
            "ok": all(s["healthy"] for s in services) if services else self.smoke_ok,
            "checks": self.smoke_checks, "smoke_ok": self.smoke_ok,
            "ip": self.ip, "mac": self.mac, "domain": self.domain, "site": site,
            "started": self.started, "services": services,
        }


class Prober:
    """DNS machine only. See module docstring."""

    def __init__(self, domain: str, self_ip: str, interval: float = PROBE_INTERVAL_S) -> None:
        self.domain, self.self_ip, self.interval = domain, self_ip, interval
        self._prev: dict[str, str] = {}
        self._stale: dict[str, bool] = {}
        self._served: dict[str, int] = {}
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 -- a prober bug must not kill the agent
                self._record("prober", "DOWN", f"prober crashed: {exc}", None)
            self._stop.wait(self.interval)

    def _record(self, name: str, st: str, detail: str, ms: float | None, **extra) -> None:
        now = time.time()
        with dashboard._lock:
            prev = dashboard.PROBES.get(name)
            since = prev["since"] if prev and prev["state"] == st else now
            dashboard.PROBES[name] = {"state": st, "detail": detail, "ms": None if ms is None else round(ms, 1),
                                      "ts": now, "since": since, **extra}
        prev_state = self._prev.get(name)
        if prev_state != st and not (st == "UNKNOWN" and prev_state is None):
            ms_txt = f" {round(ms)}ms" if ms is not None else ""
            team_logs.emit(self.self_ip, self.self_ip, "probe", f"{st} {name} {detail}{ms_txt}")
        self._prev[name] = st

    def run_once(self) -> None:
        data = state.load()
        edge_ip = data["peers"].get("edge")
        host = f"app.{self.domain}"
        now = time.time()
        with dashboard._lock:
            hbs = {k: dict(v) for k, v in dashboard.HEARTBEATS.items()}

        for role, hb in hbs.items():
            stale = now - hb["ts"] > dashboard.STALE_AFTER_S
            if stale != self._stale.get(role, False):
                msg = (f"LOST agent {role} {hb.get('ip', '?')}: no heartbeat for {int(now - hb['ts'])}s "
                       "(machine off, agent stopped, or network cut)") if stale else \
                      f"BACK agent {role} {hb.get('ip', '?')} is reporting again"
                team_logs.emit(self.self_ip, self.self_ip, "probe", msg)
            self._stale[role] = stale

        r = netprobe.dns_query_a(host, self.self_ip, timeout=2)
        if not r.ok:
            self._record("dns", "DOWN", f"{host}: {r.error}", None)
        elif edge_ip and edge_ip not in r.addrs:
            self._record("dns", "DOWN", f"{host} -> {r.addrs} but the edge is {edge_ip}", r.rtt_ms)
        else:
            self._record("dns", "UP", f"{host} -> {r.addrs[0]} via {self.self_ip}", r.rtt_ms)

        if not edge_ip:
            self._record("lb", "UNKNOWN", "edge IP not known yet", None)
            self._record("lb-app", "UNKNOWN", "edge IP not known yet", None)
        else:
            e = netprobe.http_fetch(edge_ip, 443, "/_edge/health", host=host, tls=True, timeout=3)
            if e.error or e.status != 200:
                self._record("lb", "DOWN", f"{edge_ip}:443 {e.error or 'HTTP ' + str(e.status)}", None)
            else:
                self._record("lb", "UP", f"{edge_ip}:443 nginx answering", e.total_ms)
            a = netprobe.http_fetch(edge_ip, 443, "/healthz", host=host, tls=True, timeout=4,
                                    headers={"Cache-Control": "no-cache"})
            if a.error:
                self._record("lb-app", "DOWN", f"via LB: {a.error}", None)
            elif a.status != 200:
                self._record("lb-app", "DOWN", f"via LB: HTTP {a.status} -- the load balancer has no healthy backend to send to", a.total_ms)
            else:
                who = a.headers.get("x-backend", "?")
                self._served[who] = self._served.get(who, 0) + 1
                self._record("lb-app", "UP", f"via LB: served by backend {who}", a.total_ms, served_by=dict(self._served))

        targets: dict[str, tuple[str, int]] = {}
        for role, hb in hbs.items():
            if role == state.ROLE_BACKENDS:
                targets["backend_a"] = (hb["ip"], 3001)
                targets["backend_b"] = (hb["ip"], 3002)
            elif role in (state.ROLE_BACKEND_A, state.ROLE_BACKEND_B) and hb.get("ip"):
                targets[role] = (hb["ip"], BACKEND_PORTS[role])
        for name, port in BACKEND_PORTS.items():
            if name not in targets:
                self._record(name, "UNKNOWN", "no heartbeat from that machine yet", None)
                continue
            ip, port = targets[name]
            b = netprobe.http_fetch(ip, port, "/healthz", timeout=3)
            if b.error:
                self._record(name, "DOWN", f"{ip}:{port} {b.error}", None)
            elif b.status != 200:
                self._record(name, "DOWN", f"{ip}:{port} HTTP {b.status}", b.total_ms)
            else:
                self._record(name, "UP", f"{ip}:{port} healthy", b.total_ms)
