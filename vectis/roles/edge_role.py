
from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import questionary
from jinja2 import Environment, FileSystemLoader

from .. import certs, state, ui
from ..netinfo import is_valid_ipv4
from .base import Role, SetupResult

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

NGINX_CONF = Path("/opt/homebrew/etc/nginx/servers/vectis.conf")


def restart_nginx(nginx_bin: str, interactive: bool = True) -> None:
    """(Re)start nginx as root WITHOUT `brew services`: `sudo brew services
    restart` hangs on macOS (see dns_role.py) and may not even be allowed to
    run as root. nginx daemonizes itself, so running it directly under sudo
    is enough, and a reload picks up the new config. `sudo -n` (interactive
    =False) never prompts -- for background callers that cannot ask."""
    sudo = ["sudo"] if interactive else ["sudo", "-n"]
    subprocess.run(["brew", "services", "stop", "nginx"], check=False, capture_output=True)
    running = subprocess.run(["pgrep", "-x", "nginx"], capture_output=True).returncode == 0
    if running and subprocess.run(sudo + [nginx_bin, "-s", "reload"], capture_output=True).returncode == 0:
        return
    if running:
        subprocess.run(sudo + ["pkill", "-x", "nginx"], check=False)
        time.sleep(1)
    subprocess.run(sudo + [nginx_bin], check=True)


class EdgeRole(Role):
    name = "edge"

    def collect_peers(self) -> dict[str, str]:
        a = questionary.text("Backend A machine's IP:", validate=is_valid_ipv4).ask()
        b = questionary.text("Backend B machine's IP:", validate=is_valid_ipv4).ask()
        return {"backend_a": a, "backend_b": b}

    def configure(self, peers: dict[str, str]) -> None:
        data = state.load()
        domain = data["domain"]
        slug = re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_")

        crt, key = certs.generate(domain)
        server = certs.publish(crt, bind_ip=data["self"]["ip"])
        print(f"\nCert published. On every OTHER machine, run:\n  {server.pull_command}\n"
              f"(the private key stays on this machine -- only the .crt is served)")
        questionary.text("Press enter once every machine has pulled it...").ask()

        env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
        rendered = env.get_template("nginx.conf.j2").render(
            domain=domain, domain_slug=slug,
            backend_a_ip=peers["backend_a"], backend_b_ip=peers["backend_b"],
            https_port=443, cert_path=str(crt), key_path=str(key),
            access_log=str(state.LOG_DIR / "nginx_trail.log"),
            error_log=str(state.LOG_DIR / "nginx_error.log"),
        )
        for name in ("nginx_trail.log", "nginx_error.log"):
            (state.LOG_DIR / name).touch()
        NGINX_CONF.parent.mkdir(parents=True, exist_ok=True)
        NGINX_CONF.write_text(rendered)
        nginx_bin = shutil.which("nginx")
        if not nginx_bin:
            raise RuntimeError("nginx not found on PATH -- `brew install nginx`")
        subprocess.run(["nginx", "-t"], check=True)
        print("\nnginx needs root to bind port 443 -- enter your password if asked:")
        subprocess.run(["sudo", "-v"], check=True)
        restart_nginx(nginx_bin)

    def smoke_test(self) -> SetupResult:
        data = state.load()
        checks: dict[str, bool] = {}
        with ui.spinner(f"Checking https://{data['domain']}/api/status ..."):
            try:
                with urllib.request.urlopen(f"https://{data['domain']}/api/status", timeout=5) as r:
                    checks["https_reachable"] = r.status == 200
            except OSError:
                checks["https_reachable"] = False
        return SetupResult(ok=all(checks.values()), checks=checks, messages=[])
