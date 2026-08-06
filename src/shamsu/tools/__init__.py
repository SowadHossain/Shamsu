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

from shamsu.tools.base import Tool
from shamsu.tools.gateway import ApprovalCallback, ToolGateway, deny_all
from shamsu.tools.readonly import (
    CodeSearchTool,
    FileReadTool,
    ProjectInspectTool,
    read_only_tools,
    summarise_manifest,
)

__all__ = [
    "ApprovalCallback",
    "CodeSearchTool",
    "FileReadTool",
    "ProjectInspectTool",
    "Tool",
    "ToolGateway",
    "deny_all",
    "read_only_tools",
    "summarise_manifest",
]
