"""`vectis trace` -- follow real requests from THIS machine and show each
step as the client experiences it: DNS answer, what this Mac's own
resolver says (the one a browser uses), TCP, TLS, then the HTTP response
including which backend answered. Complements `vectis trail`, which
shows the same requests from the servers' side.
"""
from __future__ import annotations

import socket

from rich.table import Table

from . import certs, netprobe, state
from .ui import console


def target() -> tuple[str | None, str | None]:
    """(dns_ip, domain) from this machine's role setup or a `vectis join`."""
    data = state.load()
    dns_ip = data["peers"].get("dns") or data["joined"].get("dns")
    domain = data.get("domain") or data["joined"].get("domain")
    return dns_ip, domain


def _cafile(domain: str) -> str | None:
    for p in (certs.pulled_path(domain), certs.CERT_DIR / f"{domain}.crt"):
        if p.exists():
            return str(p)
    return None


def system_resolve(host: str) -> tuple[str | None, str | None]:
    """What getaddrinfo (browsers, curl, ping) returns -- honours
    /etc/resolver on macOS, unlike `dig`."""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        return infos[0][4][0], None
    except socket.gaierror as exc:
        return None, str(exc)


def run(dns_ip: str, domain: str, count: int = 6, path: str = "/", name: str | None = None) -> bool:
    host = name or f"app.{domain}"
    console.print(f"[bold]Trace of https://{host}{path}[/bold]  ({count} request{'s' if count != 1 else ''})\n")

    dns = netprobe.dns_query_a(host, dns_ip)
    if not dns.ok:
        console.print(f"[red]DNS      FAILED[/red]  asked {dns_ip}: {dns.error}")
        console.print("         The vectis DNS server is unreachable or doesn't know that name. Nothing after DNS can work.")
        return False
    ip = dns.addrs[0]
    console.print(f"[cyan]DNS     [/cyan] asked {dns_ip} (vectis DNS): {host} -> [bold]{ip}[/bold]  ttl {dns.ttl}s  {dns.rtt_ms:.2f}ms")
    sys_ip, sys_err = system_resolve(host)
    if sys_ip == ip:
        console.print(f"[cyan]        [/cyan] this Mac's own resolver agrees ({sys_ip}) [green]OK[/green] -- a browser here will find it")
    else:
        what = f"says {sys_ip}" if sys_ip else f"fails ({sys_err})"
        console.print(f"[cyan]        [/cyan] [yellow]this Mac's own resolver {what}[/yellow] -- a browser/curl here will NOT reach "
                      f"the page. Run: vectis join --dns {dns_ip} --domain {domain}   (tracing continues using the vectis DNS answer)")

    cafile = _cafile(domain)
    served: dict[str, int] = {}
    first_detail = True
    table = Table(show_header=True, header_style="bold", pad_edge=False)
    for c in ("#", "status", "backend", "upstream", "tcp", "tls", "1st byte", "request id"):
        table.add_column(c, no_wrap=True)
    ok_all = True
    for i in range(1, count + 1):
        r = netprobe.http_fetch(ip, 443, path, host=host, tls=True, verify=True, cafile=cafile,
                                headers={"Cache-Control": "no-cache"})
        if r.error:
            ok_all = False
            console.print(f"[red]request {i} FAILED[/red]: {r.error}")
            if r.tls_verified is False:
                console.print(f"         Run: vectis join --dns {dns_ip} --domain {domain}   (pulls and trusts the cert)")
            continue
        if first_detail:
            first_detail = False
            console.print(f"[cyan]TCP     [/cyan] connected to {ip}:443 in {r.connect_ms:.1f}ms")
            how = "cert pulled from the edge" if cafile else "system trust store"
            console.print(f"[cyan]TLS     [/cyan] {r.tls_version} {r.tls_cipher}, certificate verified OK ({how}), "
                          f"valid for {', '.join(r.cert_sans) or '?'}  {r.tls_ms:.1f}ms\n")
        backend = r.headers.get("x-backend", "?")
        served[backend] = served.get(backend, 0) + 1
        ok_all = ok_all and r.ok
        color = "green" if r.ok else "red"
        table.add_row(str(i), f"[{color}]{r.status}[/{color}]", f"[bold]{backend}[/bold]",
                      r.headers.get("x-upstream", "-"), f"{r.connect_ms:.1f}ms",
                      f"{r.tls_ms:.1f}ms" if r.tls_ms is not None else "-", f"{r.ttfb_ms:.1f}ms",
                      r.headers.get("x-request-id", "-")[:8])
    if table.row_count:
        console.print(table)
    if served:
        console.print("\nserved by: " + "   ".join(f"Backend {k}: {v}" for k, v in sorted(served.items())))
        if len(served) == 1 and count >= 4:
            console.print("[yellow]only one backend answered -- the other may be down (check `vectis trail`)[/yellow]")
    console.print("[dim]Find these requests server-side in `vectis trail` by request id.[/dim]")
    return ok_all
