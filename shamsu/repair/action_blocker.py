"""Block repeated, non-productive repair actions.

The failure this prevents: the model proposes the same write/patch to the
same file(s), it doesn't reduce the error, and the loop tries the identical
thing again. We fingerprint each action (its target files + change payload)
and refuse to run an action whose fingerprint already failed to make
progress, forcing a different strategy or a stop.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def action_signature(files_changed: list[str], payload: str) -> str:
    """Stable fingerprint of a proposed action: which files, what content."""
    normalized_files = ",".join(sorted(f.replace("\\", "/") for f in files_changed))
    digest = hashlib.sha256()
    digest.update(normalized_files.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(payload.encode("utf-8"))
    return digest.hexdigest()


@dataclass
class RepeatedActionBlocker:
    max_repeats: int = 1  # a signature may be attempted once; a repeat is blocked
    _failed: dict[str, int] = field(default_factory=dict)

    def is_blocked(self, signature: str) -> bool:
        """True if this exact action already failed to make progress and must
        not be retried verbatim."""
        return self._failed.get(signature, 0) >= self.max_repeats

    def record_failure(self, signature: str) -> None:
        """Mark an action as having been tried without progress."""
        self._failed[signature] = self._failed.get(signature, 0) + 1

    def reset(self, signature: str) -> None:
        """Clear an action's history after it made progress (state changed,
        so an equivalent action may be legitimate later)."""
        self._failed.pop(signature, None)
