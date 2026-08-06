"""Path sandbox, command policy, and secret redaction.

Note the limit, as in v1: this is a policy layer, not an OS sandbox. It
constrains what *tool arguments* can address. Workspace isolation proper --
resource limits, restricted filesystem and network, no host Docker socket --
is a deployment concern described in plan section 24.1.

See plan section 24.
"""

from shamsu.security.paths import PathEscape, PathSandbox

__all__ = ["PathEscape", "PathSandbox"]
