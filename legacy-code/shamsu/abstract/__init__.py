"""SHAMSU's adapter/service layer around the external Codebase-Memory MCP tool.

This package intentionally contains no code graph, AST parser, or dependency
graph of its own. All structural code facts (symbols, imports, exports, call
graph, impact) come from the real upstream `codebase-memory-mcp` binary via
`shamsu.tools.codebase_memory.CodebaseMemoryAdapter`. This package only adds
the thin orchestration SHAMSU needs: health gating, workspace status files,
and auto build/refresh bookkeeping.
"""
