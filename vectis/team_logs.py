"""Team-wide log aggregation.

Every configured machine tails its own local sources (the exact same
follower used by `vectis logs` -- see live_logs._follow) and forwards
each line to the DNS machine, which appends it to one shared file:
timestamped, tagged with the source machine's IP and which service the
line came from. `vectis logs --team`, on any machine, tails that one
file the same way it tails a local one -- same follower, same
isolation guarantees, just pointed at a different path. That gives a
single trailing view across the whole deployment instead of having to
go machine to machine, the way a hosted platform's log viewer does.

Reuses dashboard.py's ThreadingHTTPServer pattern exactly -- already
proven to work, no reason to do this differently.
"""
from __future__ import annotations

import collections
import json
import queue
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import live_logs, state

AGGREGATOR_PORT = 9193
TEAM_LOG = state.LOG_DIR / "team.log"


_EVENTS: "collections.deque[dict]" = collections.deque(maxlen=20000)
_seq = 0
_ev_lock = threading.Lock()


def _append(ip: str, source: str, line: str) -> None:
    global _seq
    TEAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with _ev_lock:
        _seq += 1
        _EVENTS.append({"seq": _seq, "ts": now, "ip": ip, "source": source, "line": line})
        with open(TEAM_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S', time.localtime(now))}  {ip:>15}  {source:<14}| {line}\n")


def _tail_json(query: str) -> dict:
    q = urllib.parse.parse_qs(query)
    since = int(q.get("since", ["-1"])[0])
    backlog = min(int(q.get("backlog", ["0"])[0]), 2000)
    with _ev_lock:
        cur = _seq
        if since < 0:
            events = list(_EVENTS)[-backlog:] if backlog else []
        else:
            events = [e for e in _EVENTS if e["seq"] > since][:1000]
    return {"seq": cur, "events": events}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        pass

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/tail":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.dumps(_tail_json(parsed.query)).encode()
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/log":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
            _append(body["ip"], body["source"], body["line"])
        except Exception:  # noqa: BLE001 -- a malformed line must not crash the aggregator
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()


def start_aggregator(bind_ip: str) -> ThreadingHTTPServer:
    """DNS / dns_edge roles only."""
    httpd = ThreadingHTTPServer((bind_ip, AGGREGATOR_PORT), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def fetch(dns_ip: str, since: int = -1, backlog: int = 0) -> dict | None:
    """Poll the aggregator. None means it is unreachable (callers should
    show that as an error, not silently retry forever)."""
    url = f"http://{dns_ip}:{AGGREGATOR_PORT}/tail?since={since}&backlog={backlog}"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def emit(dns_ip: str, ip: str, source: str, line: str) -> None:
    """Send one event straight to the aggregator (no log file involved, so
    no follower-start race). Used for deploy / health-change events."""
    _push(dns_ip, ip, source, line)


def _push(dns_ip: str, ip: str, source: str, line: str) -> None:
    body = json.dumps({"ip": ip, "source": source, "line": line}).encode()
    req = urllib.request.Request(
        f"http://{dns_ip}:{AGGREGATOR_PORT}/log", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2):
            pass
    except OSError:
        pass


def start_forwarding(role: str, dns_ip: str, self_ip: str) -> threading.Event:
    """Every role runs this, DNS included (it forwards to itself at
    its own IP, same code path as everyone else -- no special case).
    Tails this machine's local sources with the same follower `vectis
    logs` uses, and pushes each line onward instead of (or alongside)
    printing it locally."""
    stop = threading.Event()
    sources = live_logs.SOURCES_BY_ROLE.get(role, {})
    out: "queue.Queue[tuple[str, str]]" = queue.Queue()
    for name, path in sources.items():
        threading.Thread(target=live_logs._follow, args=(name, path, out, stop), daemon=True).start()

    def consume() -> None:
        while not stop.is_set():
            try:
                name, line = out.get(timeout=1)
            except queue.Empty:
                continue
            _push(dns_ip, self_ip, name, line)

    threading.Thread(target=consume, daemon=True).start()
    return stop
