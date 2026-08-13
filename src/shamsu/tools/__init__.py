"""The typed tool gateway.

One registry, not two. Every tool declares typed input and output, a risk
level, the phases it is allowed in, whether it is reversible, what evidence it
produces, and which artifacts it invalidates. The gateway enforces all of it;
none of it is advisory.

Two properties worth stating plainly:

* **The model is only shown tools reachable in the current phase.** A
  wrong-phase call is therefore a runtime bug, not an expected model mistake,
  and gets fixed in the runtime rather than patched with prompt text.
* **Output is capped before it enters any context**, never after. A
  budget-aware trimmer always keeps the most recent message, so one oversized
  read would survive trimming and crowd out everything else.

Milestone 5. See plan sections 22, 23.
"""

from pathlib import Path
from typing import Any

from shamsu.tools.base import Tool
from shamsu.tools.checks import CheckRunTool
from shamsu.tools.editing import FilePatchTool, PatchUndo
from shamsu.tools.gateway import ApprovalCallback, ToolGateway, deny_all
from shamsu.tools.git import GitCheckpointTool, GitInspectTool, rollback_to, run_git
from shamsu.tools.readonly import (
    CodeSearchTool,
    FileListTool,
    FileReadTool,
    ProjectInspectTool,
    read_only_tools,
    summarise_manifest,
)
from shamsu.tools.testing import TestRunTool

__all__ = [
    "ApprovalCallback",
    "CheckRunTool",
    "CodeSearchTool",
    "FileListTool",
    "FilePatchTool",
    "FileReadTool",
    "GitCheckpointTool",
    "GitInspectTool",
    "PatchUndo",
    "ProjectInspectTool",
    "TestRunTool",
    "Tool",
    "ToolGateway",
    "authoring_tools",
    "deny_all",
    "read_only_tools",
    "rollback_to",
    "run_git",
    "summarise_manifest",
]


def authoring_tools(workspace: "Path", *, use_git: bool = True) -> "list[Tool[Any]]":
    """The full surface: read-only tools plus editing and verification.

    Assembled in one place so a caller cannot accidentally build a gateway with
    write tools but no way to verify what it wrote.
    """
    return [
        *read_only_tools(workspace, use_git=use_git),
        FilePatchTool(workspace),
        GitInspectTool(workspace),
        GitCheckpointTool(workspace),
        TestRunTool(workspace),
        CheckRunTool(workspace),
    ]
