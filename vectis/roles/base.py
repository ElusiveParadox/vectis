from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class SetupResult:
    ok: bool
    checks: dict[str, bool]
    messages: list[str]

class Role(ABC):
    name: str

    @abstractmethod
    def collect_peers(self) -> dict[str, str]:
        pass

    @abstractmethod
    def configure(self, peers: dict[str, str]) -> None:
        pass

    @abstractmethod
    def smoke_test(self) -> SetupResult:
        pass
