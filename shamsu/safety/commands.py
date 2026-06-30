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
    r"-----BEGIN.*PRIVATE KEY",
    r"password\s*=\s*['\"][^'\"]+",
    r"api_key\s*=\s*['\"][^'\"]+",
    r"secret\s*=\s*['\"][^'\"]+",
    r"SECRET_KEY\s*=\s*['\"][^'\"]+",         # Django-specific
    r"postgresql://[^@]*:[^@]*@",
    r"mysql://[^@]*:[^@]*@",
]


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
