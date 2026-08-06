"""Versioned, source-traceable repository artifacts.

The primary long-codebase compression mechanism. Every artifact carries source
paths, source hashes, a generator version, and a freshness status, so a stale
structural claim can never silently reach the model.

Milestone 3. See plan sections 14-17.
"""
