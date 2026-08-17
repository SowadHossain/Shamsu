"""
shamsu/safety/commands.py — Dev C owns this file.

Command risk classification + secret pattern detection. These lists
get extended throughout the project — treat them as living constants,
not a one-time Day 1 task.
"""
from __future__ import annotations

import re
from shamsu.types import CommandRisk

SAFE_COMMANDS = {
    "pytest", "python -m pytest", "python manage.py test",
    "npm test", "npm run build", "npm run dev",
    "git status", "git diff", "git log --oneline",
    "make test",
}

MEDIUM_COMMANDS = {
    "pip install", "pip3 install", "python -m pip install",
    "python -m venv", "uv venv", "uv pip install",
    "npm install", "poetry add",
    "git checkout", "git merge",
    "python manage.py migrate", "python manage.py makemigrations",
    "python manage.py runserver",
}

BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+[/~]",
    r"sudo", r"\bsu\s",
    r"chmod\s+-R\s+777",
    r"dd\s+if=", r"mkfs",
    r"shutdown", r"reboot",
    r"kill\s+-9\s+-1",
    r":\(\)\{.*\}",
    r"curl.*\|\s*bash",
    r"wget.*\|\s*sh",
    r">\s*/dev/sd",
]

SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",                      # AWS key
    r"sk-[a-zA-Z0-9]{32,}",                   # OpenAI-style key
    r"ghp_[a-zA-Z0-9]{36}",                   # GitHub token
    r"-----BEGIN.*PRIVATE KEY[^-]*-----[\s\S]+?-----END.*PRIVATE KEY[^-]*-----",
    r"-----BEGIN.*PRIVATE KEY",
    r"password\s*=\s*['\"][^'\"]+",
    r'"password"\s*:\s*"[^"]+"',
    r"api_key\s*=\s*['\"][^'\"]+",
    r'"api_key"\s*:\s*"[^"]+"',
    r"secret\s*=\s*['\"][^'\"]+",
    r'"secret"\s*:\s*"[^"]+"',
    r"token\s*=\s*['\"][^'\"]+",
    r'"token"\s*:\s*"[^"]+"',
    r"SECRET_KEY\s*=\s*['\"][^'\"]+",         # Django-specific
    r'"SECRET_KEY"\s*:\s*"[^"]+"',             # Django-specific JSON logs
    r"[Aa]uthorization\s*:\s*(Bearer|Basic|Token)\s+\S+",  # HTTP auth headers
    r'"[Aa]uthorization"\s*:\s*"[^"]+"',
    r"postgresql://[^@]*:[^@]*@",
    r"mysql://[^@]*:[^@]*@",
    r"mongodb(\+srv)?://[^@]*:[^@]*@",
    # Unquoted assignments (`export API_KEY=abc`, `--token=abc`, `password: abc`).
    # The quoted forms above miss these, which is the shape a secret actually
    # takes in a pasted prompt or a shell command - and full prompts/CoT are now
    # written to .shamsu/runs/, so an unredacted value would land on disk.
    # Placed last: the quoted patterns consume their key name first, so these
    # only ever see what those left behind.
    r"(api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|token)\s*[=:]\s*[^\s'\";,)]{4,}",
]


# Action types that MAY be auto-approved once the user chooses to remember
# a decision for them (see shamsu/safety/permission_store.py). Shell commands,
# deletions, and external network actions are never auto-approvable here,
# regardless of remembered choices — those always go through approval_func.
AUTO_APPROVABLE_ACTION_TYPES = {"file_write", "file_edit"}


def is_auto_approvable_action(action_type: str) -> bool:
    return action_type in AUTO_APPROVABLE_ACTION_TYPES


# Shell syntax and commands that can write into the current workspace. This is
# intentionally conservative: read-only requests may run tests and programs,
# but they may not smuggle a write past the file-tool guard via the shell.
_SHELL_WRITE_RE = re.compile(
    r"(?:^|\s)(?:>>?|[12]>>?|&>)\s*[^&|]"
    r"|\b(?:tee|out-file|set-content|add-content|new-item|remove-item|move-item|"
    r"copy-item|touch|mkdir|rmdir|del|erase|move|copy|rm|mv|cp)\b"
    r"|\b(?:pip3?\s+install|python3?\s+-m\s+(?:pip\s+install|venv)|"
    r"uv\s+(?:venv|sync|add|pip\s+install)|poetry\s+(?:add|install)|npm\s+install)\b",
    re.IGNORECASE,
)


def command_may_write_workspace(command: str) -> bool:
    """Return True when shell syntax visibly writes files or directories."""
    return bool(_SHELL_WRITE_RE.search(command or ""))


# Git subcommands that cannot change anything, whatever flags follow.
_READ_ONLY_GIT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "describe",
     "shortlog", "blame", "cat-file", "symbolic-ref"}
)
# Subcommands that read in one form and MUTATE in another. Each maps to the
# flag set that keeps it read-only; a bare invocation (no arguments) also reads.
# `git branch --show-current` reads; `git branch -D old` deletes a branch.
_CONDITIONAL_GIT_READS: dict[str, frozenset[str]] = {
    "branch": frozenset({"--show-current", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose"}),
    "remote": frozenset({"-v", "--verbose", "show"}),
    "config": frozenset({"--get", "--get-all", "--list", "-l"}),
    "stash": frozenset({"list", "show"}),
    "tag": frozenset({"-l", "--list"}),
}


def is_read_only_git(cmd: str) -> bool:
    """Whether *cmd* is a git query that cannot modify the repository.

    `project.inspect` runs `git branch --show-current` and
    `git rev-parse --is-inside-work-tree` to describe a workspace. Neither was
    in SAFE_COMMANDS, so both fell through to "unknown -> MEDIUM" and stopped
    the agent for manual approval: live 2026-08-17, a single "create the base
    files" turn raised two approval prompts to ask git which branch it was on,
    and both were spent before any file was written. A read that needs
    permission is a read the agent learns not to do.
    """
    parts = (cmd or "").strip().split()
    if len(parts) < 2 or parts[0].lower() != "git":
        return False
    index = 1
    while index + 1 < len(parts) and parts[index] in {"-C", "-c", "--git-dir", "--work-tree"}:
        index += 2  # skip `git -C <path> ...` style global options
    if index >= len(parts):
        return False
    subcommand = parts[index].lower()
    rest = [part for part in parts[index + 1:] if part != "--"]
    if subcommand in _READ_ONLY_GIT_SUBCOMMANDS:
        return True
    allowed = _CONDITIONAL_GIT_READS.get(subcommand)
    if allowed is None:
        return False
    if not rest:
        return True  # bare `git branch` / `git remote` lists, it does not write
    if subcommand in {"branch", "tag"}:
        # EVERY argument must be a listing flag: a bare name creates
        # (`git branch feature`) or deletes (`git branch -D feature`).
        return all(part in allowed for part in rest)
    # `remote`/`config`/`stash` take a read verb first, then its operands:
    # `git config --get user.name` reads, `git config user.name value` writes.
    return rest[0] in allowed


def classify_command(cmd: str) -> CommandRisk:
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return CommandRisk.BLOCKED
    normalized = _classification_view(cmd)
    if any(normalized.startswith(safe) for safe in SAFE_COMMANDS):
        return CommandRisk.SAFE
    if is_read_only_git(normalized):
        return CommandRisk.SAFE
    if any(normalized.startswith(medium) for medium in MEDIUM_COMMANDS):
        return CommandRisk.MEDIUM
    return CommandRisk.MEDIUM  # unknown commands default to requiring approval


def _classification_view(command: str) -> str:
    """Normalize known project-environment wrappers without weakening policy."""
    normalized = command.strip()
    normalized = re.sub(r"^(?:poetry|uv)\s+run\s+", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(
        r"""^(?:"[^"]*\.venv[\\/](?:Scripts|bin)[\\/]python(?:\.exe)?"
        |[^\s"']*\.venv[\\/](?:Scripts|bin)[\\/]python(?:\.exe)?)""",
        "python",
        normalized,
        flags=re.IGNORECASE | re.VERBOSE,
    )
    normalized = re.sub(
        r"""^(?:"[^"]*\.venv[\\/](?:Scripts|bin)[\\/]pip(?:\.exe)?"
        |[^\s"']*\.venv[\\/](?:Scripts|bin)[\\/]pip(?:\.exe)?)""",
        "pip",
        normalized,
        flags=re.IGNORECASE | re.VERBOSE,
    )
    return normalized.lower()


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = re.sub(pattern, "[REDACTED]", text, flags=re.IGNORECASE)
    return text
