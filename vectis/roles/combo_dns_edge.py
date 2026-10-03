from __future__ import annotations
from .base import Role, SetupResult
from .dns_role import DnsRole
from .edge_role import EdgeRole

class DnsEdgeRole(Role):
    name = 'dns_edge'

    def __init__(self) -> None:
        self._dns = DnsRole()
        self._edge = EdgeRole()

    def collect_peers(self) -> dict[str, str]:
        peers = {'edge': '127.0.0.1'}
        peers.update(self._edge.collect_peers())
        return peers

    def configure(self, peers: dict[str, str]) -> None:
        self._dns.configure(peers)
        self._edge.configure(peers)

    def smoke_test(self) -> SetupResult:
        d = self._dns.smoke_test()
        e = self._edge.smoke_test()
        return SetupResult(ok=d.ok and e.ok, checks={**d.checks, **e.checks}, messages=d.messages + e.messages)
