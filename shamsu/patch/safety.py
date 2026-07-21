"""
Extra safety gates for the Patch/File Mutation Engine, layered on top of
shamsu.safety.sandbox.Sandbox (path traversal / symlink escape / outside
workspace). This module adds mutation-specific rules: never touch .git
internals, never silently edit files that look like secrets, and always
require explicit approval for destructive operations.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from shamsu.safety.sandbox import Sandbox, SecurityError

SECRET_FILENAME_PATTERNS = [
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),
    re.compile(r"^\.npmrc$", re.IGNORECASE),
    re.compile(r"^\.pypirc$", re.IGNORECASE),
    re.compile(r"^credentials(\.\w+)?$", re.IGNORECASE),
    re.compile(r"^secrets?(\.\w+)?$", re.IGNORECASE),
    re.compile(r"id_rsa(\.pub)?$", re.IGNORECASE),
    re.compile(r"id_ed25519(\.pub)?$", re.IGNORECASE),
    re.compile(r".*\.pem$", re.IGNORECASE),
    re.compile(r".*\.key$", re.IGNORECASE),
    re.compile(r".*\.pfx$", re.IGNORECASE),
    re.compile(r".*\.p12$", re.IGNORECASE),
    re.compile(r".*service[-_]account.*\.json$", re.IGNORECASE),
]


class MutationSafetyError(Exception):
    pass


def is_git_internal_path(relative_path: str) -> bool:
    posix_parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    return bool(posix_parts) and posix_parts[0] == ".git"


def is_secret_file(relative_path: str) -> bool:
    name = PurePosixPath(relative_path.replace("\\", "/")).name
    return any(pattern.match(name) for pattern in SECRET_FILENAME_PATTERNS)


def is_trash_path(relative_path: str) -> bool:
    posix_parts = PurePosixPath(relative_path.replace("\\", "/")).parts
    return len(posix_parts) >= 2 and posix_parts[0] == ".shamsu" and posix_parts[1] == "trash"


def reject_unsafe_relative_path(relative_path: str) -> None:
    if not relative_path or not relative_path.strip():
        raise MutationSafetyError("Path is empty.")
    posix_path = PurePosixPath(relative_path)
    windows_path = PureWindowsPath(relative_path)
    if posix_path.is_absolute() or windows_path.is_absolute():
        raise MutationSafetyError(f"Path must be relative to the workspace: {relative_path}")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise MutationSafetyError(f"Path escapes workspace: {relative_path}")


def validate_mutation_path(sandbox: Sandbox, relative_path: str) -> Path:
    """Sandbox-validate a path for a file mutation and reject .git internals.

    Sandbox.validate() already resolves symlinks/traversal via Path.resolve();
    this layers on the mutation-specific ".git internals are off-limits" rule.
    """
    reject_unsafe_relative_path(relative_path)
    try:
        target = sandbox.validate(relative_path)
    except SecurityError as exc:
        raise MutationSafetyError(str(exc)) from exc
    if is_git_internal_path(relative_path):
        raise MutationSafetyError(f"Refusing to edit .git internals: {relative_path}")
    return target
