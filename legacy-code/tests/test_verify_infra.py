"""Container and reverse-proxy config is code, and nothing checked it.

A `docker-compose.yml` with a mis-shaped `ports` entry and an `nginx.conf` with
an unclosed block are both confident, plausible and completely broken, and
every check SHAMSU had - py_compile, npm build, Django system checks, the
template probe - is blind to them. Generating infra without a verifier is
theatre.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.verify import infra
from shamsu.verify.gate import build_verification_plan

VALID_COMPOSE = "services:\n  web:\n    image: nginx:alpine\n    ports:\n      - '8080:80'\n"
INVALID_COMPOSE = "services:\n  web:\n    image: nginx:alpine\n    ports: 'unterminated\n"


def _compose(root: Path, body: str = VALID_COMPOSE, name: str = "docker-compose.yml") -> Path:
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


# ── discovery ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
)
def test_every_standard_compose_filename_is_found(tmp_path: Path, name: str):
    _compose(tmp_path, name=name)

    assert infra.compose_files(tmp_path, [name])


def test_a_compose_file_in_a_subdirectory_is_found(tmp_path: Path):
    (tmp_path / "deploy").mkdir()
    _compose(tmp_path / "deploy")

    assert infra.compose_files(tmp_path, ["deploy/docker-compose.yml"])


def test_a_windows_style_changed_path_is_found(tmp_path: Path):
    (tmp_path / "deploy").mkdir()
    _compose(tmp_path / "deploy")

    assert infra.compose_files(tmp_path, ["deploy\\docker-compose.yml"])


def test_a_compose_file_named_in_the_change_but_absent_is_ignored(tmp_path: Path):
    assert infra.compose_files(tmp_path, ["docker-compose.yml"]) == []


def test_nginx_config_is_recognised_by_name(tmp_path: Path):
    (tmp_path / "nginx.conf").write_text("events {}\n", encoding="utf-8")

    assert infra.nginx_files(tmp_path, ["nginx.conf"])


def test_an_unrelated_conf_file_is_not_treated_as_nginx(tmp_path: Path):
    """`setup.conf` is not a reverse proxy."""
    (tmp_path / "setup.conf").write_text("[tool]\n", encoding="utf-8")

    assert infra.nginx_files(tmp_path, ["setup.conf"]) == []


# ── planning ──────────────────────────────────────────────────────────────


def test_compose_validation_is_planned(tmp_path: Path):
    _compose(tmp_path)

    stages = [s.stage for s in build_verification_plan(tmp_path, ["docker-compose.yml"]).steps]

    assert "infra" in stages


def test_the_compose_command_needs_no_daemon(tmp_path: Path):
    """`config -q` parses client-side, so it works with Docker Desktop stopped."""
    path = _compose(tmp_path)

    command = infra.compose_command(path, tmp_path)

    assert command == 'docker compose -f "docker-compose.yml" config -q'
    assert "up" not in command and "build" not in command


def test_the_parser_is_skipped_without_docker_but_structure_is_still_checked(
    tmp_path: Path, monkeypatch
):
    """A missing tool skips its own check and never fails the work - but the
    structural lint is pure Python, so it still runs on a machine with no
    Docker at all."""
    _compose(tmp_path)
    monkeypatch.setattr(infra, "docker_available", lambda: False)

    commands = [s.command for s in build_verification_plan(tmp_path, ["docker-compose.yml"]).steps]

    assert not any("docker compose" in c for c in commands)
    assert any("_compose_lint" in c for c in commands)


def test_the_structural_lint_catches_what_the_parser_cannot(tmp_path: Path):
    """`docker compose config -q` exits 0 on a depends_on/service_healthy that
    points at a service with no healthcheck - verified against the real CLI.
    It then fails at `up`. That gap is the whole reason this lint exists."""
    body = (
        "services:\n"
        "  a:\n    image: nginx:alpine\n"
        "  b:\n    image: nginx:alpine\n"
        "    depends_on:\n      a:\n        condition: service_healthy\n"
    )
    _compose(tmp_path, body)
    lint = infra.write_compose_lint(tmp_path)

    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(lint), "docker-compose.yml"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "defines no healthcheck" in result.stdout


def test_the_structural_lint_passes_a_consistent_file(tmp_path: Path):
    body = (
        "services:\n"
        "  a:\n    image: nginx:alpine\n"
        "    healthcheck:\n      test: ['CMD', 'true']\n"
        "  b:\n    image: nginx:alpine\n"
        "    depends_on:\n      a:\n        condition: service_healthy\n"
        "    volumes:\n      - data:/var/lib/x\n"
        "volumes:\n  data:\n"
    )
    _compose(tmp_path, body)
    lint = infra.write_compose_lint(tmp_path)

    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(lint), "docker-compose.yml"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout


def test_the_structural_lint_catches_an_undeclared_named_volume(tmp_path: Path):
    body = (
        "services:\n"
        "  a:\n    image: nginx:alpine\n"
        "    volumes:\n      - ./local.conf:/etc/nginx/nginx.conf:ro\n"
        "      - pgdata:/var/lib/postgresql/data\n"
    )
    _compose(tmp_path, body)
    lint = infra.write_compose_lint(tmp_path)

    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(lint), "docker-compose.yml"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "pgdata" in result.stdout
    # A bind mount is not a named volume and must not be reported.
    assert "local.conf" not in result.stdout


def test_nginx_is_skipped_when_the_binary_is_absent(tmp_path: Path, monkeypatch):
    (tmp_path / "nginx.conf").write_text("events {}\n", encoding="utf-8")
    monkeypatch.setattr(infra, "nginx_available", lambda: False)

    commands = [s.command for s in build_verification_plan(tmp_path, ["nginx.conf"]).steps]

    assert not any("nginx" in c for c in commands)


def test_nginx_check_is_advisory_when_available(tmp_path: Path, monkeypatch):
    """A local nginx build often lacks the modules a production config uses, so
    a failure here is a warning rather than a verdict on the change."""
    (tmp_path / "nginx.conf").write_text("events {}\n", encoding="utf-8")
    monkeypatch.setattr(infra, "nginx_available", lambda: True)

    steps = [s for s in build_verification_plan(tmp_path, ["nginx.conf"]).steps if s.stage == "infra"]

    assert steps and all(step.required is False for step in steps)


def test_a_project_with_no_infra_files_plans_no_infra_checks(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

    stages = [s.stage for s in build_verification_plan(tmp_path, ["app.py"]).steps]

    assert "infra" not in stages
