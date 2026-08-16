"""Runtime wrapper around deterministic verification checks."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from shamsu.session.manager import SessionLogger
from shamsu.verify.gate import VerifyOutcome, verify_only


class ChangeVerifier:
    def __init__(
        self,
        workspace_root: Path,
        *,
        command_runner: Any,
        session_logger: SessionLogger | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.command_runner = command_runner
        self.session_logger = session_logger

    async def verify(self, written_files: list[str]) -> VerifyOutcome:
        return await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: verify_only(
                self.workspace_root,
                list(written_files),
                command_runner=self.command_runner,
                lightweight=True,
                session_logger=self.session_logger,
            ),
        )
