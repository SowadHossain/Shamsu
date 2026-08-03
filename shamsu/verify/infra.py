"""Verification for container and reverse-proxy configuration.

Infrastructure config is the easiest thing in a project to generate and the
hardest to check by reading. A `docker-compose.yml` with a mis-shaped `ports`
entry, or an `nginx.conf` with an unclosed block, is confident, plausible, and
completely broken - and every check SHAMSU had (py_compile, npm build, Django
system checks) is blind to it. Generating infra without a verifier is theatre:
the model writes two hundred lines of YAML, the gate reports success, and
nothing has been established at all.

Both checks here are deliberately **daemon-free**:

* ``docker compose config -q`` parses and validates the file client-side. It
  exits 0 on a valid file and 1 on an invalid one without contacting the Docker
  daemon, so it works on a machine where Docker Desktop is not running.
* ``nginx -t`` needs an nginx binary, which is rarely installed on a developer
  laptop. When one is absent the check is *skipped*, not failed - an absent
  tool is a gap in the harness, never a defect in the work.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Sequence

COMPOSE_FILENAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
NGINX_SUFFIXES = (".conf",)
NGINX_NAME_HINTS = ("nginx", "default.conf", "site.conf")


def _posix(value: str) -> str:
    return str(value).replace("\\", "/")


def compose_files(workspace: Path, changed: Sequence[str]) -> list[Path]:
    """Compose files touched by this change, or present at the workspace root."""
    found: list[Path] = []
    for value in changed:
        name = _posix(value).rsplit("/", 1)[-1]
        if name in COMPOSE_FILENAMES:
            candidate = workspace / _posix(value)
            if candidate.is_file():
                found.append(candidate)
    if not found:
        for name in COMPOSE_FILENAMES:
            candidate = workspace / name
            if candidate.is_file():
                found.append(candidate)
                break
    return list(dict.fromkeys(found))


def nginx_files(workspace: Path, changed: Sequence[str]) -> list[Path]:
    """Changed files that look like nginx configuration."""
    found: list[Path] = []
    for value in changed:
        posix = _posix(value)
        name = posix.rsplit("/", 1)[-1].lower()
        if not name.endswith(NGINX_SUFFIXES):
            continue
        if not any(hint in posix.lower() for hint in NGINX_NAME_HINTS):
            continue
        candidate = workspace / posix
        if candidate.is_file():
            found.append(candidate)
    return list(dict.fromkeys(found))


def docker_available() -> bool:
    return shutil.which("docker") is not None


def nginx_available() -> bool:
    return shutil.which("nginx") is not None


def compose_command(path: Path, project_root: Path) -> str:
    """Client-side validation of *path*. No daemon required."""
    try:
        relative = path.relative_to(project_root).as_posix()
    except ValueError:
        relative = path.as_posix()
    return f'docker compose -f "{relative}" config -q'


# `docker compose config -q` is a *parser*. It happily accepts
# `depends_on: {web: {condition: service_healthy}}` pointing at a service that
# defines no healthcheck - which then fails at `up` with "service web is
# required to be healthy but has no healthcheck". Verified against the real
# Docker CLI on 2026-08-03: exit 0. The same syntax-versus-semantics gap that
# let dead Django code report "verified"; the parser is necessary, not
# sufficient.
COMPOSE_LINT = r'''
import sys, pathlib
try:
    import yaml
except ImportError:
    print("compose-lint: PyYAML is unavailable; structural checks skipped")
    sys.exit(0)

path = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "docker-compose.yml")
try:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
except Exception as exc:
    print("compose-lint: could not read %s: %s" % (path, exc)); sys.exit(1)

services = document.get("services") or {}
declared_volumes = set((document.get("volumes") or {}).keys())
failures = []

for name, service in services.items():
    service = service or {}
    depends = service.get("depends_on") or {}
    # Long form only: the short list form cannot express a condition.
    if isinstance(depends, dict):
        for target, spec in depends.items():
            if target not in services:
                failures.append("%s depends_on '%s', which is not a service" % (name, target))
                continue
            condition = (spec or {}).get("condition") if isinstance(spec, dict) else None
            if condition == "service_healthy" and not (services.get(target) or {}).get("healthcheck"):
                failures.append(
                    "%s waits for '%s' to be service_healthy, but '%s' defines no healthcheck"
                    % (name, target, target)
                )
    elif isinstance(depends, list):
        for target in depends:
            if target not in services:
                failures.append("%s depends_on '%s', which is not a service" % (name, target))

    for volume in service.get("volumes") or []:
        if not isinstance(volume, str) or ":" not in volume:
            continue
        source = volume.split(":", 1)[0]
        # A bind mount starts with . or / or a drive letter; anything else is a
        # named volume and must be declared at the top level.
        if source.startswith((".", "/", "~")) or (len(source) > 1 and source[1] == ":"):
            continue
        if source not in declared_volumes:
            failures.append("%s mounts named volume '%s', which is not declared" % (name, source))

if failures:
    print("COMPOSE STRUCTURE FAILURES:")
    for item in failures:
        print(" - " + item)
    sys.exit(1)
print("compose structure ok: %d service(s)" % len(services))
'''


def write_compose_lint(project_root: Path) -> Path:
    target = Path(project_root) / ".shamsu_compose_lint.py"
    target.write_text(COMPOSE_LINT, encoding="utf-8")
    return target


def compose_lint_command(path: Path, project_root: Path, python_bin: str) -> str:
    try:
        relative = path.relative_to(project_root).as_posix()
    except ValueError:
        relative = path.as_posix()
    return f'{python_bin} .shamsu_compose_lint.py "{relative}"'


def nginx_command(path: Path, project_root: Path) -> str:
    try:
        relative = path.relative_to(project_root).as_posix()
    except ValueError:
        relative = path.as_posix()
    return f'nginx -t -c "{relative}"'
