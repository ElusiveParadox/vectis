"""`vectis join` -- let a Mac that has no vectis role view the app.

Does exactly what a client needs and nothing else: point *.<domain> at
the team DNS server (/etc/resolver, needs sudo), pull the edge's public
certificate and trust it, then verify with a real request. It needs the
vectis package installed on the joining Mac (pip install), but no role,
no brew formulae and no setup.
"""
from __future__ import annotations

import urllib.request

from rich.markup import escape

from . import certs, netprobe, state, sysdns, trace
from .netinfo import is_valid_ipv4
from .ui import console

CERT_PORT = 9190


def run(dns_ip: str, domain: str) -> bool:
    if not is_valid_ipv4(dns_ip):
        console.print(f"[red]'{dns_ip}' is not an IPv4 address.[/red]")
        return False
    host = f"app.{domain}"
    console.print(f"[bold]1/4[/bold] Asking {dns_ip} where {host} lives...")
    r = netprobe.dns_query_a(host, dns_ip)
    if not r.ok:
        console.print(f"[red]  {r.error}[/red]\n  Check the DNS machine's IP, the domain spelling, and that its vectis agent is running.")
        return False
    edge_ip = r.addrs[0]
    console.print(f"  {host} -> {edge_ip} (that's the load balancer)")

    console.print(f"[bold]2/4[/bold] Pointing *.{domain} at {dns_ip} on this Mac (sudo)...")
    ok, msg = sysdns.enable(domain, dns_ip)
    console.print(f"  [{'green' if ok else 'red'}]{escape(msg)}[/{'green' if ok else 'red'}]")
    if not ok:
        return False

    console.print(f"[bold]3/4[/bold] Pulling the certificate from the load balancer ({edge_ip}:{CERT_PORT})...")
    dest = certs.pulled_path(domain)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(f"http://{edge_ip}:{CERT_PORT}/{domain}.crt", timeout=5) as resp:
            pem = resp.read()
    except OSError as exc:
        console.print(f"[red]  Could not fetch it: {exc}[/red]\n  The edge machine serves the cert while its vectis agent runs; "
                      "start/restart `vectis setup` there.")
        return False
    if b"BEGIN CERTIFICATE" not in pem:
        console.print("[red]  What came back isn't a certificate.[/red]")
        return False
    dest.write_bytes(pem)
    ok, msg = certs.trust(domain)
    console.print(f"  [{'green' if ok else 'red'}]{escape(msg)}[/{'green' if ok else 'red'}]")

    state.update(joined={"dns": dns_ip, "domain": domain})
    if not state.load().get("domain"):
        state.update(domain=domain)

    console.print("[bold]4/4[/bold] Verifying with a real request...\n")
    good = trace.run(dns_ip, domain, count=2)
    sys_ip, _ = trace.system_resolve(host)
    if good and sys_ip != edge_ip:
        console.print(f"\n[bold yellow]Requests work, but this Mac's own resolver doesn't use the vectis DNS yet[/bold yellow] "
                      f"(it says {sys_ip or 'nothing'} for {host}, expected {edge_ip}). A browser here would still fail. "
                      f"Check `scutil --dns | grep -B2 -A4 {domain}`, then reconnect Wi-Fi or flush the cache "
                      "(sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder) and rerun this command.")
        return False
    if good:
        console.print(f"\n[bold green]Joined.[/bold green] Open https://{host}/ in a browser, or run `vectis trail` to watch requests.")
        console.print("[dim]Note: `dig` ignores /etc/resolver on macOS; use a browser, curl, or `vectis trace` to test.[/dim]")
    return good
