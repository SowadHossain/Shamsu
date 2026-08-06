"""Run-id generation for ActionLedger."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone


def new_run_id(now: datetime | None = None) -> str:
    """Unique, roughly-sortable run id, e.g. run_2026-07-06_14-31-22_ab12.

    Sortable at second resolution (matches the session-id scheme in
    shamsu/session/manager.py::_new_session_id) - two runs started in
    different seconds always sort chronologically; two runs started in the
    same second are merely unique, not ordered relative to each other.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d_%H-%M-%S")
    return f"run_{stamp}_{uuid.uuid4().hex[:4]}"
