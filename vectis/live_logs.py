
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from rich.markup import escape

from . import state
from .ui import console

SOURCE_COLORS = {
    "dnsmasq": "cyan",
    "nginx-access": "magenta",
    "nginx-error": "red",
    "backend_a": "green",
    "backend_b": "yellow",
    "fault-events": "bold red",
    "supervisor": "blue",
    "hosted-service": "bright_green",
    "health": "bright_cyan",
    "probe": "bright_cyan",
    "agent": "bright_blue",
}


_ALWAYS_ON = {
    "fault-events": state.LOG_DIR / "fault.log",
    "supervisor": state.LOG_DIR / "supervisor.log",
}

SOURCES_BY_ROLE: dict[str, dict[str, Path]] = {
    state.ROLE_DNS: {
        "dnsmasq": state.LOG_DIR / "dnsmasq.log",
        "hosted-service": state.LOG_DIR / "hosted_service.log",
        **_ALWAYS_ON,
    },
    state.ROLE_EDGE: {
        "nginx-access": state.LOG_DIR / "nginx_trail.log",
        "nginx-error": state.LOG_DIR / "nginx_error.log",
        **_ALWAYS_ON,
    },
    state.ROLE_BACKEND_A: {"backend_a": state.LOG_DIR / "backend_a.log", **_ALWAYS_ON},
    state.ROLE_BACKEND_B: {"backend_b": state.LOG_DIR / "backend_b.log", **_ALWAYS_ON},
    state.ROLE_DNS_EDGE: {
        "dnsmasq": state.LOG_DIR / "dnsmasq.log",
        "nginx-access": state.LOG_DIR / "nginx_trail.log",
        "nginx-error": state.LOG_DIR / "nginx_error.log",
        **_ALWAYS_ON,
    },
    state.ROLE_BACKENDS: {
        "backend_a": state.LOG_DIR / "backend_a.log",
        "backend_b": state.LOG_DIR / "backend_b.log",
        **_ALWAYS_ON,
    },
}


def _follow(name: str, path: Path, out: "queue.Queue[tuple[str, str]]", stop: threading.Event) -> None:
    """tail -f semantics: seeks to end-of-file on first open, so only
    lines written *after* this follower first sees the file are shown
    -- pre-existing content is never replayed. For a file that doesn't
    exist yet, this means the very first line written at the instant
    the file is created can race the next poll tick and be missed;
    every subsequent line is not. That's an acceptable trade for a
    live operational view (you want what's happening now, not a
    replay), and is called out here since it's easy to mistake for a bug."""
    reported_waiting = False
    fh = None
    try:
        while not stop.is_set():
            if fh is None:
                if not path.exists():
                    if not reported_waiting:
                        out.put((name, f"[waiting for {path.name} to appear]"))
                        reported_waiting = True
                    time.sleep(1)
                    continue
                fh = open(path, "r", errors="replace")
                fh.seek(0, 2)
            line = fh.readline()
            if line:
                out.put((name, line.rstrip("\n")))
            else:
                time.sleep(0.25)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see module docstring
        out.put((name, f"[log follower stopped: {exc}]"))


def _render(name: str, line: str, ip: str | None) -> None:
    """Vercel/Render-style trailing line: dim timestamp, this
    machine's IP (so a screenshot or a shared terminal is
    unambiguous about whose log it is), colored source badge, then
    the line itself. Team-aggregated lines already carry their own
    IP column (see team_logs.py), so `ip` is None there -- adding a
    second one would be redundant."""
    ts = time.strftime("%H:%M:%S")
    color = SOURCE_COLORS.get(name, "white")
    ip_part = f"[bold]{ip:>15}[/bold]  " if ip else ""
    console.print(f"[dim]{ts}[/dim]  {ip_part}[{color}]{name:<14}[/{color}]| {line}")


def _tail(sources: dict[str, Path], ip: str | None) -> None:
    if not sources:
        console.print("No known log sources.")
        return
    out: "queue.Queue[tuple[str, str]]" = queue.Queue()
    stop = threading.Event()
    for name, path in sources.items():
        threading.Thread(target=_follow, args=(name, path, out, stop), daemon=True).start()

    console.print(f"[bold]Tailing:[/bold] {', '.join(sources)}  [dim](Ctrl+C to stop)[/dim]\n")
    try:
        while True:
            name, line = out.get()
            _render(name, line, ip)
    except KeyboardInterrupt:
        stop.set()
        console.print("\n[dim]Stopped.[/dim]")


def run(role: str) -> None:
    sources = SOURCES_BY_ROLE.get(role, {})
    if not sources:
        console.print(f"No known log sources for role '{role}'.")
        return
    self_ip = state.load()["self"]["ip"]
    _tail(sources, ip=self_ip)


def run_team(backlog: int = 20) -> None:
    """Tails the shared cross-machine log from the aggregator on the DNS
    machine, over HTTP, so it works from any machine on the LAN (it used to
    tail a local file that only existed on the DNS machine). Lines are
    already tagged with source IP and service by the forwarders."""
    from . import team_logs
    data = state.load()
    dns_ip = data["peers"].get("dns") or data["joined"].get("dns")
    if not dns_ip:
        console.print("[red]No DNS machine known.[/red] Run `vectis setup`, or `vectis join --dns <ip> --domain <d>`.")
        return
    first = team_logs.fetch(dns_ip, backlog=backlog)
    if first is None:
        console.print(f"[red]Cannot reach the log aggregator at {dns_ip}:{team_logs.AGGREGATOR_PORT}.[/red] "
                      "Is the DNS machine's agent running?")
        return
    console.print(f"[bold]Team log from {dns_ip}[/bold]  [dim](Ctrl+C to stop)[/dim]\n")
    seq, unreachable = first["seq"], False
    events = first["events"]
    try:
        while True:
            for e in events:
                color = SOURCE_COLORS.get(e["source"], "white")
                ts = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
                console.print(f"[dim]{ts}[/dim]  [bold]{e['ip']:>15}[/bold]  [{color}]{e['source']:<14}[/{color}]| {escape(e['line'])}",
                              highlight=False)
                seq = e["seq"]
            time.sleep(0.5)
            r = team_logs.fetch(dns_ip, since=seq)
            if r is None:
                if not unreachable:
                    console.print("[red]-- lost contact with the aggregator, retrying --[/red]")
                unreachable, events = True, []
                continue
            if unreachable:
                console.print("[green]-- aggregator reachable again --[/green]")
            unreachable = False
            if r["seq"] < seq:
                seq = 0
            events = r["events"]
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
