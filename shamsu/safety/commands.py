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
    "pip install", "npm install", "poetry add",
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
    r"copy-item|touch|mkdir|rmdir|del|erase|move|copy|rm|mv|cp)\b",
    re.IGNORECASE,
)


def command_may_write_workspace(command: str) -> bool:
    """Return True when shell syntax visibly writes files or directories."""
    return bool(_SHELL_WRITE_RE.search(command or ""))


def classify_command(cmd: str) -> CommandRisk:
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return CommandRisk.BLOCKED
    normalized = cmd.strip()
    if any(normalized.startswith(safe) for safe in SAFE_COMMANDS):
        return CommandRisk.SAFE
    if any(normalized.startswith(medium) for medium in MEDIUM_COMMANDS):
        return CommandRisk.MEDIUM
    return CommandRisk.MEDIUM  # unknown commands default to requiring approval


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = re.sub(pattern, "[REDACTED]", text, flags=re.IGNORECASE)
    return text
