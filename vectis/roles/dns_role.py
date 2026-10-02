from __future__ import annotations
import subprocess
from pathlib import Path
import questionary
from jinja2 import Environment, FileSystemLoader
from .. import state, ui
from ..netinfo import is_valid_ipv4
from .base import Role, SetupResult
TEMPLATE_DIR = Path(__file__).parent.parent / 'templates'
DNSMASQ_CONF = Path('/opt/homebrew/etc/dnsmasq.conf')

class DnsRole(Role):
    name = 'dns'

    def collect_peers(self) -> dict[str, str]:
        edge_ip = questionary.text("Load Balancer / Edge machine's IP address (app.<domain> will point here):", validate=is_valid_ipv4).ask()
        return {'edge': edge_ip}

    def configure(self, peers: dict[str, str]) -> None:
        data = state.load()
        domain = data['domain']
        env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
        rendered = env.get_template('dnsmasq.conf.j2').render(domain=domain, edge_ip=peers.get('edge'), hosted_ip=peers.get('hosted'), log_path=str(state.LOG_DIR / 'dnsmasq.log'))
        DNSMASQ_CONF.parent.mkdir(parents=True, exist_ok=True)
        DNSMASQ_CONF.write_text(rendered)
        subprocess.run(['sudo', '-v'], check=True)
        with ui.spinner('Restarting dnsmasq (needs sudo -- port 53 is privileged)...'):
            plist_src = '/opt/homebrew/opt/dnsmasq/homebrew.mxcl.dnsmasq.plist'
            plist_dest = '/Library/LaunchDaemons/homebrew.mxcl.dnsmasq.plist'
            if Path(plist_src).exists():
                subprocess.run(['sudo', 'cp', plist_src, plist_dest], check=True)
                subprocess.run(['sudo', 'launchctl', 'unload', '-w', plist_dest], check=False)
                subprocess.run(['sudo', 'launchctl', 'load', '-w', plist_dest], check=True)
            else:
                ui.console.print(f'[bold red]Cannot find dnsmasq plist at {plist_src}[/bold red]')

    def smoke_test(self) -> SetupResult:
        data = state.load()
        domain = data['domain']
        checks: dict[str, bool] = {}
        with ui.spinner(f'Checking DNS resolution for app.{domain}...'):
            try:
                result = subprocess.run(['dig', '@127.0.0.1', f'app.{domain}', '+short'], capture_output=True, text=True, timeout=5)
                checks['dns_resolves_app_domain'] = bool(result.stdout.strip())
            except subprocess.TimeoutExpired:
                checks['dns_resolves_app_domain'] = False
        return SetupResult(ok=all(checks.values()), checks=checks, messages=[])
