"""Path sandbox, command policy, and secret redaction.

Note the limit, as in v1: this is a policy layer, not an OS sandbox. It
constrains what *tool arguments* can address. Workspace isolation proper --
resource limits, restricted filesystem and network, no host Docker socket --
is a deployment concern described in plan section 24.1.

See plan section 24.
"""

from shamsu.security.commands import (
    classify_command,
    explain,
    is_blocked,
    writes_to_workspace,
)
from shamsu.security.paths import PathEscape, PathSandbox
from shamsu.security.secrets import contains_secret, redact, redact_structure

__all__ = [
    "PathEscape",
    "PathSandbox",
    "classify_command",
    "contains_secret",
    "explain",
    "is_blocked",
    "redact",
    "redact_structure",
    "writes_to_workspace",
]
