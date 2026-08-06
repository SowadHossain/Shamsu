"""SHAMSU v2 — a local-first autonomous coding agent.

The organising principle, stated once here because every module depends on it:

    The runtime controls the loop. The model performs one narrow decision at
    a time. Complete information lives outside the model; the context compiler
    selects only what the next decision needs.

This inverts SHAMSU v1, where the model drove the loop and the runtime reacted.
See ``docs/migration/v2-full-rebuild-plan.md`` for the full rationale and
``legacy-code/LEGACY_README.md`` for what went wrong.

Package map
-----------
``interfaces``        Protocols defining every seam. Depend on these, not on
                      concrete implementations.
``state``             Typed records and the SQLite store. SQLite is
                      authoritative for all runtime state.
``runtime``           Run controller, state machine, execution limits.
``agent``             Task classifier, planner, step executor, repair and
                      completion controllers.
``context``           The context compiler: builds compact task packets.
``artifacts``         Versioned, hash-traceable repository artifacts.
``code_intelligence`` Structural retrieval: symbols, references, call graph.
``tools``             The typed tool gateway and its contracts.
``verification``      Evidence collection and the verification pipeline.
``memory``            Project facts, architecture decisions, failure lessons.
``models``            Local model clients and output normalisation.
``security``          Path sandbox, command policy, secret redaction.
``telemetry``         Events, metrics, reliability tracking.

Invariants
----------
1. Deterministic retrieval before inference; compact packets, never raw dumps.
2. SQLite is authoritative. Artifacts are derived and invalidatable.
3. Fresh tool results override stale artifacts.
4. Completion requires verified evidence: ``required ⊆ verified``.
5. Honest failure over fabrication.
6. Local-first: no cloud inference.
7. Nothing here imports from ``legacy-code/``. Enforced by
   ``scripts/check_import_boundary.py``.
"""

__version__ = "2.0.0a0"

__all__ = ["__version__"]
