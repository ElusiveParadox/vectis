from __future__ import annotations
import os
from contextlib import contextmanager
from rich.console import Console
console = Console()
_PLAIN = os.environ.get('VECTIS_PLAIN') == '1'

@contextmanager
def spinner(message: str):
    style = 'white' if _PLAIN else 'bold cyan'
    kind = 'line' if _PLAIN else 'dots'
    with console.status(f'[{style}]{message}[/{style}]', spinner=kind):
        yield
