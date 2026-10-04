from __future__ import annotations
import datetime
import os
import shutil
import signal
import subprocess
import time
import questionary
import typer
from rich.live import Live
from rich.panel import Panel
from . import certs, control, dashboard, fault as fault_module, hosting, live_logs, netinfo, preflight, state, supervisor, sysdns, team_logs
from .roles.backend_role import BackendRole
from .roles.combo_backends import BackendsRole
from .roles.combo_dns_edge import DnsEdgeRole
from .roles.dns_role import DNSMASQ_CONF, DnsRole
from .roles.edge_role import NGINX_CONF, EdgeRole
from .ui import console, spinner
app = typer.Typer(add_completion=False, help='Deploy and operate a private DNS, edge/load-balancer, and backend network.')
_debug_enabled = False
ROLE_MENU = {'DNS server': (state.ROLE_DNS, lambda: DnsRole()), 'Edge / load balancer': (state.ROLE_EDGE, lambda: EdgeRole()), 'Backend A': (state.ROLE_BACKEND_A, lambda: BackendRole('A')), 'Backend B': (state.ROLE_BACKEND_B, lambda: BackendRole('B')), 'DNS + Edge combo': (state.ROLE_DNS_EDGE, lambda: DnsEdgeRole()), 'Backend A + B combo': (state.ROLE_BACKENDS, lambda: BackendsRole())}
SHARE_TARGETS = {state.ROLE_DNS: "every other machine (they set this as their DNS resolver, and it's where status heartbeats go)", state.ROLE_EDGE: 'the DNS machine (it needs this IP to create the app.<domain> / api.<domain> record)', state.ROLE_BACKEND_A: 'the Load Balancer / Edge machine (it needs this IP for its upstream block)', state.ROLE_BACKEND_B: 'the Load Balancer / Edge machine (it needs this IP for its upstream block)', state.ROLE_DNS_EDGE: 'Backend A and Backend B (they set this as their DNS resolver + heartbeat target)', state.ROLE_BACKENDS: "the Load Balancer / Edge machine (it needs this IP for both backends' upstream entries)"}

def _local_control_handler(action: str) -> dict:
    data = state.load()
    targets = {'stop_backend_a': 'backend_a', 'stop_backend_b': 'backend_b'}
    role_key = targets.get(action)
    if role_key is None:
        return {'ok': False, 'error': f'unknown action: {action}'}
    svc = data['services'].get(role_key)
    if not svc:
        return {'ok': False, 'error': f'{role_key} not owned by this machine'}
    os.kill(svc['pid'], signal.SIGKILL)
    return {'ok': True}

def _start_local_agent(role_id: str, info: netinfo.NetInfo, domain: str, dns_ip: str) -> None:
    control.start_listener(info.ip, _local_control_handler)
    team_logs.start_forwarding(role_id, dns_ip=dns_ip, self_ip=info.ip)
    watched: list[supervisor.WatchedService] = []
    if role_id in (state.ROLE_BACKEND_A, state.ROLE_BACKENDS):
        watched.append(supervisor.WatchedService(name='backend_a', is_up=supervisor.http_is_up('http://127.0.0.1:3001/api/status'), restart=lambda: BackendRole('A').configure({})))
    if role_id in (state.ROLE_BACKEND_B, state.ROLE_BACKENDS):
        watched.append(supervisor.WatchedService(name='backend_b', is_up=supervisor.http_is_up('http://127.0.0.1:3002/api/status'), restart=lambda: BackendRole('B').configure({})))
    if role_id in (state.ROLE_DNS, state.ROLE_DNS_EDGE):
        watched.append(supervisor.WatchedService(name='dnsmasq', is_up=supervisor.dns_is_up(domain), restart=supervisor.brew_service_restart('dnsmasq')))
    if role_id in (state.ROLE_EDGE, state.ROLE_DNS_EDGE):
        watched.append(supervisor.WatchedService(name='nginx', is_up=supervisor.process_is_up('nginx'), restart=supervisor.brew_service_restart('nginx')))
    if watched:
        supervisor.start(watched)

@app.command()
def setup(role_name: str=typer.Option(None, help="Skip the menu, e.g. --role-name 'Backend A'"), foreground: bool=typer.Option(True, help='Stay running after configuring so status heartbeats, log forwarding, fault-command listening, and auto-restart keep working. Turn off only for scripting/testing -- the agent does nothing once this process exits.')) -> None:
    _setup_impl(role_name, foreground)

def _setup_impl(role_name: str | None, foreground: bool) -> None:
    choices = list(ROLE_MENU.keys()) + ['Exit']
    choice = role_name or questionary.select('What will this machine be used as?', choices=choices).ask()
    if choice is None or choice == 'Exit':
        console.print('[dim]Cancelled -- no role changed.[/dim]')
        return
    if choice not in ROLE_MENU:
        console.print(f"[bold red]Unknown role '{choice}'.[/bold red] Choices: {list(ROLE_MENU)}")
        raise typer.Exit(1)
    role_id, role_factory = ROLE_MENU[choice]
    role = role_factory()
    domain = questionary.text('Team domain (e.g. team1.test):').ask()
    info = netinfo.detect()
    confirmed = questionary.confirm(f'Detected {info.interface}: {info.ip} ({info.mac}) -- use this?', default=True).ask()
    if not confirmed:
        info = netinfo.NetInfo(interface=questionary.text('Interface name:').ask(), ip=questionary.text('IP address:', validate=netinfo.is_valid_ipv4).ask(), mac=questionary.text('MAC address:').ask())
    state.update(domain=domain, role=role_id, self={'ip': info.ip, 'mac': info.mac, 'iface': info.interface})
    console.print(f'[bold]This machine:[/bold] {info.ip} ({info.mac})')
    console.print(f'[bold]Share it with:[/bold] {SHARE_TARGETS[role_id]}\n')
    console.print(f"[bold]Checking dependencies for '{choice}'...[/bold]")
    results = preflight.run(role_id)
    for r in results:
        style = 'green' if r.installed else 'red'
        console.print(f'  [{style}]{r.formula}: {r.action}[/{style}]')
    if not preflight.all_ok(results):
        console.print('[bold red]One or more formulae failed to install -- fix that before continuing.[/bold red]')
        raise typer.Exit(1)
    console.print('[green]All modules installed.[/green]' if results else 'No extra modules required for this role.')
    peers = role.collect_peers()
    is_dns_host = role_id in state.DASHBOARD_ROLES
    dns_ip = '127.0.0.1' if is_dns_host else questionary.text("DNS machine's IP (for the team status dashboard):", validate=netinfo.is_valid_ipv4).ask()
    state.set_peer('dns', dns_ip)
    for k, v in peers.items():
        state.set_peer(k, v)
    role.configure(peers)
    console.print('\n[bold]Running smoke test...[/bold]')
    result = role.smoke_test()
    state.record_smoke_test(result.ok, result.checks)
    for check, passed in result.checks.items():
        tag, style = ('PASS', 'green') if passed else ('FAIL', 'red')
        console.print(f'  [{style}][{tag}][/{style}] {check}')
    console.print('[bold green]All tests passed.[/bold green]' if result.ok else '[bold red]Smoke test FAILED -- see checks above.[/bold red]')
    _start_local_agent(role_id, info, domain, dns_ip)
    if is_dns_host:
        team_logs.start_aggregator(bind_ip=info.ip)
        dashboard.start_aggregator(bind_ip=info.ip)
        console.print(f'\n[bold]Team status dashboard live at[/bold] http://{info.ip}:{dashboard.HEARTBEAT_PORT}/status')
    else:
        dashboard.start_heartbeat_loop(dns_ip, role_id, lambda: {'ok': result.ok, 'checks': result.checks})
        console.print(f'\nPushing status heartbeats to DNS machine at {dns_ip} every {dashboard.HEARTBEAT_INTERVAL_S}s.')
    use_resolver = questionary.confirm(f'Resolve *.{domain} to the team DNS server from this machine too, so you can open https://app.{domain} in a browser? (needs sudo)', default=True).ask()
    if use_resolver:
        ok, message = sysdns.enable(domain, dns_ip)
        console.print(f"[{('green' if ok else 'red')}]{message}[/{('green' if ok else 'red')}]")
        if ok:
            console.print(f"[dim]Still need: 'vectis trust-cert' here before the cert stops warning.[/dim]")
    if foreground:
        _run_foreground_agent(role_id, domain)
    else:
        console.print('[dim]--no-foreground: agent threads exist only until this process exits.[/dim]')

def _agent_panel(role_id: str, domain: str, start: float) -> Panel:
    data = state.load()
    uptime = str(datetime.timedelta(seconds=int(time.monotonic() - start)))
    last = data.get('last_smoke_test') or {}
    smoke = '[green]ok[/green]' if last.get('passed') else '[red]failed[/red]' if last else 'n/a'
    body = f'role        {role_id}\ndomain      {domain}\nuptime      {uptime}\nlast check  {smoke}'
    return Panel(body, title='vectis agent -- Ctrl+C to stop', style='cyan', expand=False)

def _run_foreground_agent(role_id: str, domain: str) -> None:
    start = time.monotonic()
    with Live(_agent_panel(role_id, domain, start), console=console, auto_refresh=False) as live:
        try:
            while True:
                time.sleep(1)
                live.update(_agent_panel(role_id, domain, start), refresh=True)
        except KeyboardInterrupt:
            pass
    console.print("\n[bold]Agent stopped.[/bold] [dim]Services managed by brew or already-spawned subprocesses keep running independently -- run 'vectis reset' to fully tear this role down.[/dim]")

@app.command()
def status() -> None:
    data = state.load()
    console.print(f"role: {data['role']}")
    console.print(f"domain: {data['domain']}")
    console.print(f"peers: {data['peers']}")
    console.print(f"last smoke test: {data.get('last_smoke_test')}")

@app.command()
def logs(team: bool=typer.Option(False, '--team', help="Tail the aggregated cross-machine log instead of just this machine's own services.")) -> None:
    _logs_impl(team)

def _logs_impl(team: bool) -> None:
    if team:
        live_logs.run_team()
        return
    data = state.load()
    if not data['role']:
        console.print('[yellow]No role configured yet -- run setup first.[/yellow]')
        return
    live_logs.run(data['role'])

@app.command()
def fault() -> None:
    fault_module.run()

@app.command(name='trust-cert')
def trust_cert() -> None:
    data = state.load()
    if not data['domain']:
        console.print('[yellow]No domain configured yet -- run setup first.[/yellow]')
        return
    ok, message = certs.trust(data['domain'])
    console.print(f"[{('green' if ok else 'yellow')}]{message}[/{('green' if ok else 'yellow')}]")

@app.command()
def reset(hard: bool=typer.Option(False, '--hard', help='Also delete generated certs and the dnsmasq/nginx config files this machine wrote.')) -> None:
    data = state.load()
    if not data['role']:
        console.print('[yellow]No role configured -- nothing to reset.[/yellow]')
        return
    for name, svc in data.get('services', {}).items():
        pid = svc.get('pid')
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            console.print(f'  stopped {name} (pid {pid})')
        except ProcessLookupError:
            pass
    if hard:
        subprocess.run(['sudo', '-v'], check=True)
        with spinner('Stopping dnsmasq and nginx...'):
            for formula in ('dnsmasq', 'nginx'):
                try:
                    plist_dest = f'/Library/LaunchDaemons/homebrew.mxcl.{formula}.plist'
                    subprocess.run(['sudo', 'launchctl', 'unload', '-w', plist_dest], check=False)
                except FileNotFoundError:
                    pass
        if certs.CERT_DIR.exists():
            shutil.rmtree(certs.CERT_DIR)
            console.print(f'  deleted {certs.CERT_DIR}')
        for path in (DNSMASQ_CONF, NGINX_CONF):
            if path.exists():
                path.unlink()
                console.print(f'  deleted {path}')
        domain = data['domain']
        if domain and sysdns.is_enabled(domain):
            ok, message = sysdns.disable(domain)
            console.print(f'  {message}')
    state.save(dict(state.DEFAULT_STATE))
    console.print(f"[green]Role reset.[/green]{(' Certs and config also removed.' if hard else '')}")
HOST_ACTIONS = ['Static site (folder of files)', 'Monorepo web service', 'Back']

@app.command()
def host() -> None:
    data = state.load()
    if data['role'] not in (state.ROLE_DNS, state.ROLE_DNS_EDGE):
        console.print(f"[bold red]Hosting requires a DNS role[/bold red] (dns or dns_edge); this machine is '{data['role']}'.")
        return
    action = questionary.select('Host what?', choices=HOST_ACTIONS).ask()
    if action is None or action == 'Back':
        return
    ip = data['self']['ip']
    if action == 'Static site (folder of files)':
        folder = questionary.text('Path to the static site folder:').ask()
        try:
            hosting.host_static(folder)
        except ValueError as exc:
            console.print(f'[bold red]{exc}[/bold red]')
            return
        ok = hosting.check_reachable(hosting.STATIC_PORT)
        _report_hosted(ok, ip, hosting.STATIC_PORT)
        if ok:
            console.print('\n[dim]Press Ctrl+C to stop hosting.[/dim]')
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                console.print('\n[dim]Stopping static site server...[/dim]')
    elif action == 'Monorepo web service':
        cwd = questionary.text('Working directory to run it from (e.g. apps/web inside the monorepo):').ask()
        command = questionary.text('Command to start it (make sure it reads $PORT to bind -- vectis sets that env var for you):').ask()
        port_str = questionary.text('Port it listens on:', validate=lambda v: v.isdigit() or 'Enter a number').ask()
        try:
            hosting.host_monorepo_service(cwd, command, int(port_str))
        except ValueError as exc:
            console.print(f'[bold red]{exc}[/bold red]')
            return
        with spinner('Waiting for the service to come up...'):
            time.sleep(1.5)
        ok = hosting.check_reachable(int(port_str))
        _report_hosted(ok, ip, int(port_str))

def _report_hosted(ok: bool, ip: str, port: int) -> None:
    style = 'green' if ok else 'red'
    console.print(f"[{style}]{('Live' if ok else 'NOT reachable yet -- check vectis logs')} at http://{ip}:{port}/[/{style}]")
    console.print('Reachable from Backend A, Backend B, and every other team machine directly -- same LAN, no proxying required.')
    if not ok:
        return
    data = state.load()
    edge_ip = data['peers'].get('edge', '127.0.0.1')
    try:
        DnsRole().configure({'edge': edge_ip, 'hosted': ip})
        console.print(f"[dim]Also reachable as http://hosted.{data['domain']}:{port}/[/dim]")
    except Exception as exc:
        console.print(f"[dim]Reachable by IP; couldn't register hosted.{data['domain']} ({exc}).[/dim]")
MENU_ACTIONS = ['Setup / change role', 'Status', 'Live logs', 'Fault injection', 'Trust certificate', 'Host on DNS', 'Reset role', 'Quit']

@app.callback(invoke_without_command=True)
def main_menu(ctx: typer.Context, debug: bool=typer.Option(False, '--debug', help='Show full tracebacks on error instead of a short message')) -> None:
    global _debug_enabled
    _debug_enabled = debug
    if ctx.invoked_subcommand is not None:
        return
    console.print(Panel.fit('vectis -- private network orchestrator', style='bold cyan'))
    while True:
        data = state.load()
        console.print(f"[dim]role: {data['role'] or 'unconfigured'}   domain: {data['domain'] or '-'}[/dim]")
        action = questionary.select('What do you want to do?', choices=MENU_ACTIONS).ask()
        if action is None or action == 'Quit':
            break
        try:
            if action == 'Setup / change role':
                _setup_impl(None, True)
            elif action == 'Status':
                status()
            elif action == 'Live logs':
                _logs_impl(False)
            elif action == 'Fault injection':
                fault()
            elif action == 'Trust certificate':
                trust_cert()
            elif action == 'Host on DNS':
                host()
            elif action == 'Reset role':
                hard = questionary.confirm('Also delete certs and config (--hard)?', default=False).ask()
                reset(hard=hard)
        except typer.Exit:
            pass
        except Exception as exc:
            _report_error(exc)
        console.print()

def _report_error(exc: Exception) -> None:
    console.print(f'[bold red]Error:[/bold red] {exc}')
    if _debug_enabled:
        console.print_exception()
    else:
        console.print('[dim]Re-run with --debug for the full traceback.[/dim]')

def main() -> None:
    try:
        app()
    except typer.Exit:
        raise
    except Exception as exc:
        _report_error(exc)
        raise SystemExit(1) from None
if __name__ == '__main__':
    main()
