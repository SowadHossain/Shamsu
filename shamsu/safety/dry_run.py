"""Dry run: let the agent plan a change without letting it happen.

Before this module, `--dry-run` was deny-mode with a different name. The
headless approval script computed `approved = self._mode == "allow"`, and
"dry-run" is not "allow", so every gate said no. That produces a preview only
for actions that reach an approval gate before the agent gives up - which
create-file never does. Measured on 2026-07-20:

    Dry run only: create a file named dry_run_should_not_exist.txt ...

    -> agent called read_file, then find_file, found nothing, reported that the
       file did not exist and asked what to do next. Zero planned actions.

Denial and dry run answer different questions. Denial says "you may not do
this", and a well-behaved agent stops. A dry run says "tell me what you WOULD
do", which requires the agent to keep going - so the write has to appear to
succeed while nothing touches the disk.

So a mutating tool in dry-run mode returns a synthetic success, records what it
would have done, and writes nothing. The agent proceeds, and the recorder holds
the plan the user actually asked to see.

Commands are deliberately NOT faked: `run_command` can do anything, including
mutate files by other means, and pretending it succeeded would fabricate
output the agent then reasons about. Commands stay denied under dry run, which
is the honest answer for a mode whose whole promise is "nothing happened".
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PlannedMutation:
    """A file change the agent would have made."""

    action: str
    path: str
    detail: str = ""
    size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "path": self.path,
            "detail": self.detail,
            "size_bytes": self.size_bytes,
        }


@dataclass
class DryRunRecorder:
    """Collects what a dry run would have done, in order."""

    planned: list[PlannedMutation] = field(default_factory=list)

    def record(self, action: str, path: str, detail: str = "", size_bytes: int = 0) -> PlannedMutation:
        entry = PlannedMutation(action=action, path=path, detail=detail, size_bytes=size_bytes)
        self.planned.append(entry)
        return entry

    def as_dicts(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.planned]

    def summary(self) -> str:
        if not self.planned:
            return "Dry run complete: the agent planned no file changes."
        lines = [f"Dry run complete: {len(self.planned)} file change(s) planned, none applied."]
        lines.extend(f"  would {entry.action} {entry.path}" for entry in self.planned)
        return "\n".join(lines)


_recorder: ContextVar[DryRunRecorder | None] = ContextVar("_dry_run_recorder", default=None)


def get_recorder() -> DryRunRecorder | None:
    """The active dry-run recorder, or None when this is a real run."""
    return _recorder.get()


def active() -> bool:
    return _recorder.get() is not None


@contextmanager
def dry_run(recorder: DryRunRecorder | None = None) -> Iterator[DryRunRecorder]:
    """Run the enclosed request in dry-run mode."""
    active_recorder = recorder or DryRunRecorder()
    token = _recorder.set(active_recorder)
    try:
        yield active_recorder
    finally:
        _recorder.reset(token)
