"""Shared test configuration.

Two rules for this suite:

1. **No live model, ever.** Unit, integration, and adversarial tests run
   against `tests.fixtures.fake_model.FakeModelClient`. Live local inference is
   exercised only on a GPU-equipped development machine, never in CI and never
   on a headless VPS.
2. **No legacy imports.** `legacy-code/` is excluded from collection by
   `norecursedirs` and is not on `sys.path`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"

# Support running the suite from a source checkout without an editable install.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def repo_root() -> Path:
    """The repository root."""
    return REPO_ROOT


@pytest.fixture(autouse=True)
def _no_legacy_on_path() -> None:
    """Guard against a test accidentally making the archive importable.

    Cheap insurance: a single `sys.path.insert` in one test would otherwise
    quietly re-enable legacy imports for every test that follows it.
    """
    offending = [entry for entry in sys.path if "legacy-code" in entry]
    assert not offending, f"legacy-code must not be on sys.path: {offending}"
