"""Local web view over SHAMSU's sessions and turn streams.

Read-only by design, for now: see `server.py` for why a Stop button that cannot
stop anything is worse than no Stop button.
"""
from __future__ import annotations

from shamsu.webui.server import DEFAULT_PORT, WebPortal

__all__ = ["WebPortal", "DEFAULT_PORT"]
