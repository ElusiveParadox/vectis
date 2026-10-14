"""`vectis trail` -- one live view of the whole deployment, from any Mac.

Top panel: every machine's services -- deployed / running / health /
last error -- plus the DNS machine's end-to-end probes.
Scrolling lines above it: one block per request showing how it was
resolved and routed, and one line per service event:

  14:02:11  GET /  200  3ms  client 10.0.0.7  rid 3fa9c1d2
     1 DNS      app.team1.test -> 10.0.0.6   (10.0.0.5 answered from config, 0.1s before)
     2 LB       10.0.0.6 nginx -> 10.0.0.3:3001 = Backend A
     3 Backend  A handled it in 1.4ms

How the pieces are joined (all by facts the logs actually contain):
  request <-> backend : the X-Request-ID nginx generates and passes on
  request <-> DNS     : same client IP + same hostname, newest lookup in
                        the last 2 minutes. This is a *correlation*, not
                        proof: if the client cached the answer, there was
                        no lookup, and the trail says so.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass

from rich.console import Group
from rich.live import Live
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import dashboard, state, team_logs
from .ui import console

DNS_WINDOW_S = 120.0
SETTLE_S = 1.5
DNS_ORPHAN_S = 3.0
PORT_NAMES = {"3001": "A", "3002": "B"}

_KV = re.compile(r'(\w+)=("[^"]*"|\S+)')
_DNS_QUERY = re.compile(r"(?:(\d+) \S+ )?query\[(\w+)\] (\S+) from (\S+)")
_DNS_ANSWER = re.compile(r"(?:(\d+) \S+ )?(config|cached|reply|/etc/hosts) (\S+) is (\S+)")
_ERR_WORDS = re.compile(r"traceback|error|exception|fatal|refused|cannot|can't", re.I)


def parse_kv(line: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _KV.findall(line)}


def _short_rid(rid: str) -> str:
    return rid[:8] if rid and rid != "-" else "-"


def _ms(x: str | float | None) -> str:
    try:
        v = float(x) * 1000
    except (TypeError, ValueError):
        return "-"
    return f"{v:.0f}ms" if v >= 1 else f"{v:.1f}ms"


@dataclass
class _Dns:
    ts: float
    client: str
    name: str
    server: str
    answer: str | None = None
    claimed: bool = False
    seen: float = 0.0


class TrailView:
    def __init__(self, domain: str | None, show_probes: bool = False, show_dns_only: bool = True) -> None:
        self.domain = (domain or "").lower()
        self.show_probes = show_probes
        self.show_dns_only = show_dns_only
        self._dns_pending_serial: dict[tuple[str, str], _Dns] = {}
        self._dns_pending_name: dict[str, _Dns] = {}
        self._dns: deque[_Dns] = deque(maxlen=2000)
        self._backend_seen: dict[str, dict] = {}
        self._reqs: list[tuple[float, dict]] = []
        self._out: list[tuple[float, int, str]] = []
        self._n = 0

    def _add(self, ts: float, text: str) -> None:
        self._n += 1
        self._out.append((ts, self._n, text))

    def feed(self, ev: dict) -> None:
        src, line, ip, ts = ev["source"], ev["line"], ev["ip"], ev["ts"]
        mono = time.monotonic()
        if src == "dnsmasq":
            self._feed_dns(ev, mono)
        elif src == "nginx-access":
            kv = parse_kv(line)
            if kv.get("rid"):
                kv["_ip"], kv["_ts"] = ip, ts
                self._reqs.append((mono, kv))
        elif src in ("backend_a", "backend_b"):
            if line.startswith("req "):
                kv = parse_kv(line)
                self._backend_seen[kv.get("rid", "-")] = {**kv, "_ip": ip}
                if len(self._backend_seen) > 5000:
                    self._backend_seen.pop(next(iter(self._backend_seen)))
                if kv.get("uri", "").startswith("/healthz") and not self.show_probes:
                    return
            elif _ERR_WORDS.search(line):
                self._add(ts, f"[dim]{self._t(ts)}[/dim]  [red]ERROR[/red]    {ip:>15}  {src}: {escape(line)}")
            elif "Running on" in line or "Serving Flask" in line:
                self._add(ts, f"[dim]{self._t(ts)}  {ip:>15}  {src} process (re)started[/dim]")
        elif line.startswith("[waiting for"):
            return
        elif src in ("health", "probe", "agent"):
            self._add(ts, self._service_line(src, ev))
        elif src == "nginx-error":
            msg = self._nginx_error(line)
            if msg:
                warn_only = "[yellow]" in msg
                tag = "[yellow]WARN[/yellow] " if warn_only else "[red]ERROR[/red]"
                self._add(ts, f"[dim]{self._t(ts)}[/dim]  {tag}    {ip:>15}  nginx: {msg}")
        elif src in ("supervisor", "fault-events"):
            color = "red" if src == "fault-events" else "yellow"
            self._add(ts, f"[dim]{self._t(ts)}[/dim]  [{color}]{src.upper():<8}[/{color}] {ip:>15}  {escape(line)}")
        elif src == "hosted-service":
            self._add(ts, f"[dim]{self._t(ts)}  {ip:>15}  hosted: {escape(line)}[/dim]")

    def _feed_dns(self, ev: dict, mono: float) -> None:
        line, ip, ts = ev["line"], ev["ip"], ev["ts"]
        m = _DNS_QUERY.search(line)
        if m:
            serial, qtype, name, client = m.groups()
            name = name.lower()
            if qtype != "A" or not self._ours(name):
                return
            d = _Dns(ts=ts, client=client, name=name, server=ip, seen=mono)
            if serial:
                self._dns_pending_serial[(ip, serial)] = d
            self._dns_pending_name[name] = d
            self._dns.append(d)
            return
        m = _DNS_ANSWER.search(line)
        if m:
            serial, kind, name, answer = m.groups()
            name = name.lower()
            d = self._dns_pending_serial.pop((ip, serial), None) if serial else None
            if d is None:
                d = self._dns_pending_name.get(name)
            if d is not None and d.answer is None and "." in answer:
                d.answer = f"{answer}|{kind}"
            elif d is not None and d.answer is None and answer.upper().startswith(("NXDOMAIN", "REFUSED")):
                d.answer = f"{answer}|{kind}"

    def _nginx_error(self, line: str) -> str | None:
        """One readable line per nginx error; None = not worth showing (the
        request blocks already say which attempts failed)."""
        if "GET /healthz" in line and not self.show_probes:
            return None
        level = (re.search(r"\[(emerg|alert|crit|error|warn|notice|info)\]", line) or [None, "error"])[1]
        if level in ("notice", "info"):
            return None
        client = (re.search(r"client: ([\d.]+)", line) or [None, "?"])[1]
        up = re.search(r'upstream: "https?://(\d+\.\d+\.\d+\.\d+:\d+)', line)
        if "connect() failed" in line and up:
            why = "refused the connection" if "111" in line else "unreachable"
            return f"backend {up.group(1)} {why} (request from {client})"
        if "no live upstreams" in line:
            return f"no live backend: every backend is currently marked down (request from {client})"
        if "temporarily disabled" in line and up:
            return f"[yellow]backend {up.group(1)} marked down for 10s after failures[/yellow]"
        if level == "warn":
            return f"[yellow]warning: {escape(line[line.find(']', 30) + 1:].strip()[:200])}[/yellow]"
        if "timed out" in line and up:
            return f"backend {up.group(1)} timed out (request from {client})"
        return escape(line[:220])

    def _ours(self, name: str) -> bool:
        return bool(self.domain) and (name == self.domain or name.endswith("." + self.domain))

    def drain(self, force: bool = False) -> list[str]:
        mono = time.monotonic()
        keep: list[tuple[float, dict]] = []
        for seen, kv in self._reqs:
            host = kv.get("host", "").lower()
            dns_known = (re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) is not None or not self._ours(host)
                         or self._find_dns(kv.get("client", ""), host, kv["_ts"]) is not None)
            ready = force or (mono - seen) >= SETTLE_S or (kv["rid"] in self._backend_seen and dns_known)
            if not ready:
                keep.append((seen, kv))
                continue
            if kv.get("uri", "").startswith("/healthz") and not self.show_probes:
                continue
            self._add(kv["_ts"], self._render_request(kv, settled=force or (mono - seen) >= SETTLE_S))
        self._reqs = keep
        if self.show_dns_only:
            for d in self._dns:
                if not d.claimed and (force or mono - d.seen >= DNS_ORPHAN_S) and d.answer is not None:
                    d.claimed = True
                    if d.client in (d.server, "127.0.0.1") and not self.show_probes:
                        continue
                    ans, kind = d.answer.split("|")
                    ok = "." in ans and not ans.upper().startswith(("NXDOMAIN", "REFUSED"))
                    color = "cyan" if ok else "red"
                    tail = "" if ok else "  [red]lookup FAILED[/red]"
                    self._add(d.ts,
                        f"[dim]{self._t(d.ts)}[/dim]  [{color}]DNS     [/{color}] {d.client} asked {d.server} for "
                        f"{d.name} -> {ans}{tail}  [dim](no request reached the load balancer within "
                        f"{int(DNS_ORPHAN_S)}s)[/dim]")
        out, self._out = sorted(self._out), []
        return [text for _, _, text in out]

    @staticmethod
    def _t(ts: float) -> str:
        return time.strftime("%H:%M:%S", time.localtime(ts))

    def _service_line(self, src: str, ev: dict) -> str:
        line = ev["line"]
        first = line.split(" ", 1)[0]
        color = {"UP": "green", "BACK": "green", "DEPLOYED": "green", "DOWN": "red", "LOST": "red",
                 "FAILED": "red"}.get(first, "yellow")
        tag = {"health": "HEALTH", "probe": "PROBE", "agent": "AGENT"}[src]
        return f"[dim]{self._t(ev['ts'])}[/dim]  [bright_cyan]{tag:<8}[/bright_cyan] {ev['ip']:>15}  [{color}]{escape(line)}[/{color}]"

    def _find_dns(self, client: str, host: str, req_ts: float) -> _Dns | None:
        best = None
        for d in self._dns:
            if d.client == client and d.name == host and -2.0 <= req_ts - d.ts <= DNS_WINDOW_S and d.answer:
                if best is None or d.ts > best.ts:
                    best = d
        return best

    def _render_request(self, kv: dict, settled: bool) -> str:
        status = int(kv.get("status", "0") or 0)
        color = "red" if status >= 500 or status == 0 else "yellow" if status >= 400 else "green"
        client, host = kv.get("client", "?"), kv.get("host", "?").lower()
        rid = kv["rid"]
        lines = [
            f"[dim]{self._t(kv['_ts'])}[/dim]  [{color}]{kv.get('method', '?')} {escape(kv.get('uri', '?'))}  "
            f"{status}  {_ms(kv.get('rt'))}[/{color}]  client {client}  [dim]rid {_short_rid(rid)}[/dim]"
        ]
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
            lines.append(f"   [cyan]1 DNS    [/cyan] not used (client addressed the load balancer by IP {host})")
        else:
            d = self._find_dns(client, host, kv["_ts"])
            if d and d.answer:
                ans, kind = d.answer.split("|")
                age = max(0.0, kv["_ts"] - d.ts)
                how = {"config": "from its own config", "cached": "from its cache", "reply": "forwarded upstream"}.get(kind, kind)
                d.claimed = True
                when = "just before" if age < 1.5 else f"{age:.0f}s earlier (client reused this answer)"
                lines.append(f"   [cyan]1 DNS    [/cyan] {host} -> [bold]{ans}[/bold]   [dim]{d.server} answered {how}, {when}[/dim]")
            else:
                lines.append(f"   [cyan]1 DNS    [/cyan] [yellow]no lookup logged in the last {int(DNS_WINDOW_S)}s[/yellow] "
                             f"[dim](client cached the answer, or doesn't use this DNS server)[/dim]")
        addrs = [a.strip() for a in kv.get("upstream", "-").split(",") if a.strip()]
        stats = [s.strip() for s in kv.get("ustatus", "-").split(",") if s.strip()]
        lb = kv["_ip"]
        if not addrs or addrs == ["-"]:
            note = "answered by nginx itself (no backend involved)" if status < 500 else \
                "[red]no backend was contacted -- no usable upstream[/red]"
            lines.append(f"   [magenta]2 LB     [/magenta] {lb} nginx: {note}")
        else:
            parts = []
            for i, a in enumerate(addrs):
                st = stats[i] if i < len(stats) else "-"
                if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+:\d+", a):
                    parts.append("[red]no usable backend: nginx has marked them all down[/red]")
                    continue
                who = PORT_NAMES.get(a.rsplit(":", 1)[-1], "?")
                ok = st.isdigit() and int(st) < 500
                why = "no valid response (refused / timed out)" if st in ("-", "502", "504") else f"HTTP {st}"
                mark = "[green]ok[/green]" if ok else f"[red]failed: {why}[/red]"
                parts.append(f"{a} = Backend {who}: {mark}")
            lines.append(f"   [magenta]2 LB     [/magenta] {lb} nginx -> " + "  then retried -> ".join(parts))
        b = self._backend_seen.get(rid)
        last_ok = bool(addrs) and addrs != ["-"] and stats and stats[-1].isdigit() and int(stats[-1]) < 500
        if b:
            lines.append(f"   [green]3 Backend[/green] {b['backend']} ({b['_ip']}) handled it in {b.get('ms', '?')}ms "
                         f"[dim](confirmed in its own log)[/dim]")
        elif last_ok and settled:
            lines.append("   [yellow]3 Backend[/yellow] answered, but its own log line hasn't arrived "
                         "[dim](that machine's log forwarding may be behind or off)[/dim]")
        elif addrs and addrs != ["-"] and not last_ok:
            lines.append("   [red]3 Backend[/red] no healthy backend answered")
        return "\n".join(lines)


def build_panel(status: dict | None, dns_ip: str) -> Group:
    if status is None:
        return Group(Text(f"Cannot reach the DNS machine's status service at {dns_ip}:{dashboard.HEARTBEAT_PORT} "
                          "-- is its vectis agent running?", style="bold red"))
    t = Table(title=f"vectis deployment  (via {dns_ip})", title_justify="left", header_style="bold",
              show_lines=False, pad_edge=False)
    for col in ("MACHINE", "ROLE", "SERVICE", "DEPLOYED", "RUNNING", "HEALTH", "DETAIL"):
        t.add_column(col, overflow="fold")
    if not status["machines"]:
        t.add_row("-", "-", "-", "-", "-", "-", "no machine has reported yet")
    for m in status["machines"]:
        ip, role = m.get("ip", "?"), m["role"]
        if m["stale"]:
            t.add_row(ip, role, "-", "yes", "?", "[red]NO CONTACT[/red]",
                      f"[red]no heartbeat for {int(m['age'])}s -- machine or agent is down[/red]")
            continue
        deployed = "[green]yes[/green]" if m.get("smoke_ok", m.get("ok")) else "[yellow]yes, smoke test FAILED[/yellow]"
        site = m.get("site")
        for i, s in enumerate(m.get("services", [])):
            run = "[green]yes[/green]" if s["running"] else "[red]NO[/red]"
            if s["healthy"]:
                health = f"[green]UP[/green] {s['ms']}ms" if s["ms"] is not None else "[green]UP[/green]"
                detail = escape(s["detail"])
            else:
                health, detail = "[red]DOWN[/red]", f"[red]{escape(s['detail'])}[/red]"
            if s["name"].startswith("backend_") and i == 0 or s["name"].startswith("backend_") and site is not None:
                detail += f"  [dim]page: {'bundled site' if site == 'bundled' else 'site ' + site if site else 'built-in demo'}[/dim]"
            t.add_row(ip if i == 0 else "", role if i == 0 else "", f"{s['name']}" + (f":{s['port']}" if s["port"] else ""),
                      deployed if i == 0 else "", run, health, detail)
        if not m.get("services"):
            t.add_row(ip, role, "-", deployed, "-", "-", "")
    probes = status.get("probes", {})
    bits = []
    for name in ("dns", "lb", "lb-app", "backend_a", "backend_b"):
        p = probes.get(name)
        if not p:
            bits.append(f"[dim]{name} ?[/dim]")
            continue
        c = {"UP": "green", "DOWN": "red"}.get(p["state"], "yellow")
        extra = ""
        if name == "lb-app" and p.get("served_by"):
            extra = " (" + " ".join(f"{k}:{v}" for k, v in sorted(p["served_by"].items())) + ")"
        bits.append(f"[{c}]{name} {p['state']}[/{c}]{extra}")
    lines = [Text.from_markup("probes from DNS machine:  " + "   ".join(bits))]
    for name, p in probes.items():
        if p["state"] == "DOWN":
            lines.append(Text.from_markup(f"  [red]{name}: {escape(p['detail'])}[/red]"))
    return Group(t, *lines)


def run(dns_ip: str, domain: str | None, backlog: int = 200, show_probes: bool = False,
        panel: bool = True, once: bool = False) -> None:
    view = TrailView(domain, show_probes=show_probes)
    first = team_logs.fetch(dns_ip, backlog=backlog)
    if first is None:
        console.print(f"[red]Cannot reach the log aggregator at {dns_ip}:{team_logs.AGGREGATOR_PORT}.[/red] "
                      "Is the DNS machine's vectis agent running?")
        return
    for ev in first["events"]:
        view.feed(ev)
    seq = first["seq"]
    for line in view.drain(force=True):
        console.print(line, highlight=False)
    if once:
        console.print(build_panel(dashboard.fetch_status(dns_ip), dns_ip))
        return

    def show(live: Live | None, status) -> None:
        if live is not None:
            live.update(build_panel(status, dns_ip), refresh=True)

    status = dashboard.fetch_status(dns_ip)
    last_status = time.monotonic()
    unreachable = False
    try:
        if panel:
            with Live(build_panel(status, dns_ip), console=console, auto_refresh=False) as live:
                _loop(view, dns_ip, seq, live, lambda: dashboard.fetch_status(dns_ip), show)
        else:
            console.print("[dim]Ctrl+C to stop[/dim]")
            _loop(view, dns_ip, seq, None, lambda: None, show)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")


def _loop(view: TrailView, dns_ip: str, seq: int, live: Live | None, get_status, show) -> None:
    last_status = 0.0
    unreachable = False
    while True:
        r = team_logs.fetch(dns_ip, since=seq)
        if r is None:
            if not unreachable:
                console.print("[red]-- lost contact with the DNS machine's log aggregator, retrying --[/red]")
            unreachable = True
        else:
            if unreachable:
                console.print("[green]-- aggregator reachable again --[/green]")
            unreachable = False
            if r["seq"] < seq:
                seq = 0
            for ev in r["events"]:
                view.feed(ev)
                seq = ev["seq"]
        for line in view.drain():
            console.print(line, highlight=False)
        if live is not None and time.monotonic() - last_status >= 2.0:
            show(live, get_status())
            last_status = time.monotonic()
        time.sleep(0.4)
