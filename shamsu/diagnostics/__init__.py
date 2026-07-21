"""Deterministic diagnostic/error digest layer.

Turns raw command stdout/stderr into a compact `ErrorPacket` before
anything reaches an LLM. See `shamsu/diagnostics/digest.py` for the
orchestrator and `shamsu/diagnostics/types.py` for the normalized types.
"""
from __future__ import annotations

from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.diagnostics.types import DiagnosticRecord, ErrorPacket, RecommendedSnippet

__all__ = ["DiagnosticDigest", "DiagnosticRecord", "ErrorPacket", "RecommendedSnippet"]
