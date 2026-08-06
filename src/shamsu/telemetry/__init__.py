"""Reliability metrics, computed from state rather than reported by the loop.

v1 counted what the loop believed: a counter incremented at the site that
thought it had succeeded. `false_success_rate` was therefore the rate at which
the loop *noticed* it had been wrong, which reads zero exactly when things are
worst.

Here every metric is a query over `tasks`, `evidence`, `tool_events`, and
`failures`. Nothing is incremented by the component being measured.

Milestone 9 / PR 15. See plan section 31.
"""

from shamsu.telemetry.metrics import ReliabilityMetrics, ReliabilityReport

__all__ = ["ReliabilityMetrics", "ReliabilityReport"]
