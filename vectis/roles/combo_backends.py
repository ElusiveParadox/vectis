from __future__ import annotations
from .backend_role import BackendRole
from .base import Role, SetupResult

class BackendsRole(Role):
    name = 'backends'

    def __init__(self) -> None:
        self._a = BackendRole('A')
        self._b = BackendRole('B')

    def collect_peers(self) -> dict[str, str]:
        return {}

    def configure(self, peers: dict[str, str]) -> None:
        self._a.configure(peers)
        self._b.configure(peers)

    def smoke_test(self) -> SetupResult:
        a = self._a.smoke_test()
        b = self._b.smoke_test()
        checks = {f'a_{k}': v for k, v in a.checks.items()}
        checks.update({f'b_{k}': v for k, v in b.checks.items()})
        return SetupResult(ok=a.ok and b.ok, checks=checks, messages=[])
